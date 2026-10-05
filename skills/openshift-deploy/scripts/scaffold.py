#!/usr/bin/env python3
"""Render the openshift-deploy templates into a repo.

    scaffold.py <repo-dir> --app NAME --stack python|node --port 8000 \
        [--health /health] [--config KEY,KEY?,...] [--secrets KEY,KEY?,...] \
        [--sentry] [--migrate] [--force]
        plain layout:     [--host-prod HOST] [--host-dev HOST]
        Kustomize layout: --tenants T1,T2,... [--host TARGET=HOST ...]

Plain layout (default): one deployment per stage, oc/dev/ and oc/prod/,
targets "dev" and "prod". With --tenants, the same app is deployed once per
tenant and stage: oc/base/ + oc/overlays/<tenant>/<stage>/ + oc/rbac/<stage>/,
targets "<tenant>-<stage>", each with its own GitHub Environment.

A key ending in "?" is optional: the Deploy workflow only sends it when the
GitHub Environment value is non-empty, so the app's own default stays in force.
Every other key must be set in every GitHub Environment or the deploy fails.

Never overwrites an existing file unless --force; prints what it wrote and
where to go next. Standard library only.
"""
import argparse
import re
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
TPL = SKILL / "templates"
REGISTRY = "registre.apps.ul-pca-pr-ul01.ulaval.ca"
NAMESPACES = {"dev": "ul-val-genovalia-dv", "prod": "ul-val-genovalia-pr"}
RBAC_FILES = ["service-account.yaml", "role.yaml", "role-binding.yaml"]
PLAIN_FILES = ["deployment.yaml", "service.yaml", "route.yaml"] + RBAC_FILES
BASE_FILES = ["kustomization.yaml", "nameref.yaml", "deployment.yaml", "service.yaml", "route.yaml"]
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
SLUG_RE = re.compile(r"[a-z](?:[a-z0-9-]*[a-z0-9])?")
RESERVED = {"OPENSHIFT_CLUSTER", "OPENSHIFT_NAMESPACE", "OPENSHIFT_TOKEN", "SLACK_BOT_TOKEN", "SENTRY_ENVIRONMENT"}
GITIGNORE_MARK = "# openshift-deploy: env files rendered by the Deploy workflow"


def parse_keys(raw: str, what: str) -> list[tuple[str, bool]]:
    keys = []
    for item in filter(None, (s.strip() for s in raw.split(","))):
        optional = item.endswith("?")
        key = item.rstrip("?")
        if not KEY_RE.match(key):
            sys.exit(f"{what}: invalid key {item!r} (UPPER_SNAKE_CASE expected)")
        if key in RESERVED:
            sys.exit(f"{what}: {key} is managed by the standard itself, do not list it")
        keys.append((key, optional))
    return sorted(keys)


def blocks(text: str, keep: dict[str, bool]) -> str:
    """Keep or drop `# >>> name` ... `# <<< name` blocks, removing the markers."""
    for name, on in keep.items():
        pattern = re.compile(rf"^# >>> {name}\n(.*?)^# <<< {name}\n", re.M | re.S)
        text = pattern.sub(lambda m: m.group(1) if on else "", text)
    return text


def subst(text: str, subs: dict[str, str]) -> str:
    for k, v in subs.items():
        text = text.replace(k, v)
    return text


def names_for(app: str, tenant: str | None, stage: str) -> dict[str, str]:
    """Resource names of one target, as deploy.py and audit.py compute them."""
    sfx = "-dev" if stage == "dev" else ""
    if tenant is None:
        name = app + sfx
        return {"name": name, "service": f"{name}-service", "route": f"{name}-route", "secret": f"{name}-secrets"}
    pre = f"{tenant}-"
    return {
        "name": f"{pre}{app}{sfx}",
        "service": f"{pre}{app}-service{sfx}",
        "route": f"{pre}{app}-route{sfx}",
        "secret": f"{pre}{app}-secrets{sfx}",
    }


def role_lists(targets: list[dict], with_secrets: bool) -> dict[str, str]:
    def q(key):
        return ", ".join(f'"{t[key]}"' for t in targets)
    out = {
        "__N_DEPLOYMENT__": q("name"),
        "__N_CONFIGMAP__": q("name"),
        "__N_IMAGE__": q("name"),
        "__N_SERVICE__": q("service"),
        "__N_ROUTE__": q("route"),
    }
    if with_secrets:
        out["__N_SECRET__"] = q("secret")
    return out


# ---------------------------------------------------------------- deploy.yml

def plain_sync(keys: list[tuple[str, bool]], source: str) -> dict[str, str]:
    req = [k for k, opt in keys if not opt]
    opt = [k for k, opt in keys if opt]
    return {
        "ENV": "\n".join(f"          {k}: ${{{{ {source}.{k} }}}}" for k, _ in keys),
        "REQUIRED": " ".join(req),
        "ARGS": "\n".join(f'            --from-literal={k}="${k}"' for k in req),
        "OPTIONAL": "\n".join(
            f'          if [ -n "${k}" ]; then args+=(--from-literal={k}="${k}"); fi' for k in opt
        ),
    }


def drop_empty_required_loop(text: str) -> str:
    return re.sub(r"^ *missing=0\n *for k in ; do\n.*?\n *done\n *\[ \"\$missing\" = 0 \]\n", "", text, flags=re.M | re.S)


def render_plain_deploy_yml(app: str, config, secrets) -> str:
    text = blocks((TPL / "workflows" / "deploy.yml").read_text(), {"secrets": bool(secrets)})
    for prefix, keys, source in (("CONFIG", config, "vars"), ("SECRET", secrets, "secrets")):
        for part, value in plain_sync(keys, source).items():
            marker = f"@@{prefix}_{part}@@"
            if part == "OPTIONAL" and not value:
                # drop the marker line and the comment that introduces it
                text = re.sub(rf"^ *# Optional keys:.*\n(?: *#.*\n)*(?= *{marker})", "", text, flags=re.M)
                text = re.sub(rf"^ *{marker}\n", "", text, flags=re.M)
            else:
                text = text.replace(marker, value)
    return drop_empty_required_loop(text).replace("__APP__", app)


def env_lines(keys: list[tuple[str, bool]]) -> str:
    out = []
    for k, opt in keys:
        line = f"printf '%s=%s\\n' {k} \"${k}\""
        out.append(f'            if [ -n "${k}" ]; then {line}; fi' if opt else f"            {line}")
    return "\n".join(out)


def render_kustomize_deploy_yml(app: str, targets: list[str], config, secrets) -> str:
    text = blocks((TPL / "workflows" / "deploy-kustomize.yml").read_text(), {"secrets": bool(secrets)})
    allkeys = [(k, o, "vars") for k, o in config] + [(k, o, "secrets") for k, o in secrets]
    subs = {
        "@@TARGET_OPTIONS@@": "\n".join(f"          - {t}" for t in targets),
        "@@ENV@@": "\n".join(f"          {k}: ${{{{ {src}.{k} }}}}" for k, _, src in sorted(allkeys)),
        "@@REQUIRED@@": " ".join(k for k, o, _ in sorted(allkeys) if not o),
        "@@CONFIG_LINES@@": env_lines(config),
        "@@SECRET_LINES@@": env_lines(secrets),
    }
    text = drop_empty_required_loop(subst(text, subs))
    return text.replace("__APP__", app)


# ---------------------------------------------------------------- main

def write(path: Path, text: str, force: bool, written: list[Path]) -> None:
    if path.exists() and not force:
        print(f"skip (exists): {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    written.append(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", type=Path)
    ap.add_argument("--app", required=True, help="kebab-case app name, e.g. ovision-api")
    ap.add_argument("--stack", required=True, choices=["python", "node"])
    ap.add_argument("--port", required=True, type=int)
    ap.add_argument("--health", default="/health", help="HTTP path for probes")
    ap.add_argument("--tenants", default="", help="Kustomize layout: comma-separated tenant names")
    ap.add_argument("--host-prod", help="plain layout, default <app>.apps.genovalia.ulaval.ca")
    ap.add_argument("--host-dev", help="plain layout, default <app>-dev.apps.genovalia.ulaval.ca")
    ap.add_argument("--host", action="append", default=[], metavar="TARGET=HOST",
                    help="Kustomize layout, default <tenant>-<app>[-dev].apps.genovalia.ulaval.ca")
    ap.add_argument("--config", default="", help="ConfigMap keys (GitHub vars)")
    ap.add_argument("--secrets", default="", help="Secret keys (GitHub secrets)")
    ap.add_argument("--sentry", action="store_true", help="pin SENTRY_ENVIRONMENT to the stage")
    ap.add_argument("--migrate", action="store_true", help="python: add a test job running alembic migrations on a real Postgres")
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    a = ap.parse_args()

    if not SLUG_RE.fullmatch(a.app) or a.app.endswith("-dev"):
        sys.exit("--app must be kebab-case and must not end in -dev")
    tenants = [t.strip() for t in a.tenants.split(",") if t.strip()]
    for t in tenants:
        if not SLUG_RE.fullmatch(t) or t in NAMESPACES:
            sys.exit(f"--tenants: invalid tenant {t!r} (kebab-case, not 'dev'/'prod')")
    if len(tenants) == 1:
        print("note: a single tenant does not need Kustomize; the plain layout is simpler")
    if a.migrate and a.stack != "python":
        sys.exit("--migrate is only for --stack python (alembic)")
    if tenants and (a.host_prod or a.host_dev):
        sys.exit("--host-prod/--host-dev are for the plain layout; use --host TARGET=HOST with --tenants")
    if a.host and not tenants:
        sys.exit("--host TARGET=HOST is for the Kustomize layout; use --host-prod/--host-dev")
    config = parse_keys(a.config, "--config")
    secrets = parse_keys(a.secrets, "--secrets")
    if not config:
        sys.exit("--config needs at least one key (the Deployment always reads its ConfigMap)")
    if {k for k, _ in config} & {k for k, _ in secrets}:
        sys.exit("a key cannot be both in --config and --secrets")

    repo: Path = a.repo
    written: list[Path] = []
    keep = {"secrets": bool(secrets), "sentry": a.sentry}
    common = {"__APP__": a.app, "__PORT__": str(a.port), "__HEALTH__": a.health}

    if not tenants:
        hosts = {
            "prod": a.host_prod or f"{a.app}.apps.genovalia.ulaval.ca",
            "dev": a.host_dev or f"{a.app}-dev.apps.genovalia.ulaval.ca",
        }
        for stage, ns in NAMESPACES.items():
            n = names_for(a.app, None, stage)
            subs = {**common, "__NAME__": n["name"], "__NS__": ns, "__ENV__": stage, "__HOST__": hosts[stage],
                    **role_lists([n], bool(secrets))}
            for f in PLAIN_FILES:
                text = subst(blocks((TPL / "oc" / f).read_text(), keep), subs)
                write(repo / "oc" / stage / f, text, a.force, written)
        deploy_yml = render_plain_deploy_yml(a.app, config, secrets)
        target_names = list(NAMESPACES)
    else:
        target_names = [f"{t}-{s}" for s in NAMESPACES for t in tenants]
        hosts = {}
        for item in a.host:
            target, _, host = item.partition("=")
            if target not in target_names or not host:
                sys.exit(f"--host {item!r}: expected TARGET=HOST with TARGET in {target_names}")
            hosts[target] = host
        for f in BASE_FILES:
            text = subst(blocks((TPL / "kustomize" / "base" / f).read_text(), keep), common)
            write(repo / "oc" / "base" / f, text, a.force, written)
        for stage, ns in NAMESPACES.items():
            stage_targets = []
            for t in tenants:
                n = names_for(a.app, t, stage)
                stage_targets.append(n)
                target = f"{t}-{stage}"
                subs = {**common, "__TARGET__": target, "__TENANT__": t, "__STAGE__": stage, "__NS__": ns,
                        "__NAME__": n["name"],
                        "__HOST__": hosts.get(target, f"{n['name']}.apps.genovalia.ulaval.ca")}
                text = blocks((TPL / "kustomize" / "overlay.yaml").read_text(), {**keep, "devsuffix": stage == "dev"})
                write(repo / "oc" / "overlays" / t / stage / "kustomization.yaml", subst(text, subs), a.force, written)
            # one CI account per namespace, covering every tenant of the stage
            subs = {**common, "__NAME__": f"{a.app}{'-dev' if stage == 'dev' else ''}", "__NS__": ns,
                    **role_lists(stage_targets, bool(secrets))}
            for f in RBAC_FILES:
                text = subst(blocks((TPL / "oc" / f).read_text(), keep), subs)
                write(repo / "oc" / "rbac" / stage / f, text, a.force, written)
        deploy_yml = render_kustomize_deploy_yml(a.app, target_names, config, secrets)
        gi = repo / ".gitignore"
        current = gi.read_text() if gi.exists() else ""
        if GITIGNORE_MARK not in current:
            gi.write_text(current + ("\n" if current and not current.endswith("\n") else "")
                          + f"{GITIGNORE_MARK} (hold secrets)\noc/overlays/*/*/config.env\noc/overlays/*/*/secret.env\n")
            written.append(gi)

    tenants_literal = "[" + ", ".join(f'"{t}"' for t in tenants) + "]"
    write(repo / "deploy.py", subst((TPL / "deploy.py").read_text(), {"__APP__": a.app, "__TENANTS__": tenants_literal}),
          a.force, written)
    wf = repo / ".github" / "workflows"
    write(wf / "deploy.yml", deploy_yml, a.force, written)
    write(wf / "restrict-main-source.yml", (TPL / "workflows" / "restrict-main-source.yml").read_text(), a.force, written)
    write(wf / "version-bump.yml", (TPL / "workflows" / f"version-bump-{a.stack}.yml").read_text(), a.force, written)
    test = blocks((TPL / "workflows" / f"test-{a.stack}.yml").read_text(), {"migrate": a.migrate, "nomigrate": not a.migrate})
    write(wf / "test.yml", test.replace("__APP__", a.app), a.force, written)

    leftovers = [p for p in written if re.search(r"__[A-Z_]+__|@@[A-Z_]+@@|^# (>>>|<<<) ", p.read_text(), re.M)]
    if leftovers:
        sys.exit(f"BUG: unreplaced placeholders in {', '.join(map(str, leftovers))}")

    print("\nwritten:")
    for p in written:
        print(f"  {p}")
    print(f"\ntargets (= GitHub Environments): {', '.join(target_names)}")
    print(
        "next: review the files (resources, strategy, probes, volumes), then follow"
        f"\n{SKILL / 'references' / 'bootstrap.md'}"
        f"\nwith APP={a.app}"
    )


if __name__ == "__main__":
    main()
