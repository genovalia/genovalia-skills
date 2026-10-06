#!/usr/bin/env python3
"""Render the openshift-deploy templates into a repo.

    scaffold.py <repo-dir> --app NAME --stack python|node --port 8000 \
        [--health /health] [--config KEY,KEY?,...] [--secrets KEY,KEY?,...] \
        [--tenants T1,T2,...] [--host TARGET=HOST ...] \
        [--sentry] [--migrate] [--force]

Always Kustomize: oc/base/ + one leaf per target + oc/rbac/<stage>/.
One tenant (default): leaves oc/dev/ and oc/prod/, targets "dev" and "prod".
With --tenants, the same app is deployed once per tenant and stage: leaves
oc/<tenant>/<stage>/, targets "<tenant>-<stage>", each with its own GitHub
Environment.

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
    """Resource names of one target, as deploy.py and audit.py compute them.
    Kustomize puts the tenant before and the stage after each base name."""
    pre = f"{tenant}-" if tenant else ""
    sfx = "-dev" if stage == "dev" else ""
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

def drop_empty_required_loop(text: str) -> str:
    return re.sub(r"^ *missing=0\n *for k in ; do\n.*?\n *done\n *\[ \"\$missing\" = 0 \]\n", "", text, flags=re.M | re.S)


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
    ap.add_argument("--tenants", default="", help="comma-separated tenant names (several tenants only)")
    ap.add_argument("--host", action="append", default=[], metavar="TARGET=HOST",
                    help="default <[tenant-]app>[-dev].apps.genovalia.ulaval.ca")
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
        print("note: a single tenant needs no --tenants: leaves are oc/dev and oc/prod")
    if a.migrate and a.stack != "python":
        sys.exit("--migrate is only for --stack python (alembic)")
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

    names_tenants: list[str | None] = tenants or [None]
    target_names = [f"{t}-{s}" if t else s for s in NAMESPACES for t in names_tenants]
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
        for t in names_tenants:
            n = names_for(a.app, t, stage)
            stage_targets.append(n)
            target = f"{t}-{stage}" if t else stage
            subs = {**common, "__TARGET__": target, "__TENANT__": t or "", "__STAGE__": stage, "__NS__": ns,
                    "__NAME__": n["name"], "__BASE__": "../../base" if t else "../base", "__HOST__": hosts.get(target, f"{n['name']}.apps.genovalia.ulaval.ca")}
            text = blocks((TPL / "kustomize" / "overlay.yaml").read_text(),
                          {**keep, "devsuffix": stage == "dev", "tenant": bool(t)})
            leaf = repo / "oc" / t / stage if t else repo / "oc" / stage
            write(leaf / "kustomization.yaml", subst(text, subs), a.force, written)
        # one CI account per namespace, covering every target of the stage
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
                      + f"{GITIGNORE_MARK} (hold secrets)\noc/*/config.env\noc/*/secret.env\n"
                        "oc/*/*/config.env\noc/*/*/secret.env\n")
        written.append(gi)

    tenants_literal = "[" + ", ".join(f'"{t}"' for t in tenants) + "]"
    write(repo / "deploy.py", subst((TPL / "deploy.py").read_text(), {"__APP__": a.app, "__TENANTS__": tenants_literal}),
          a.force, written)
    wf = repo / ".github" / "workflows"
    write(wf / "deploy.yml", deploy_yml, a.force, written)
    write(wf / "restrict-main-source.yml", (TPL / "workflows" / "restrict-main-source.yml").read_text(), a.force, written)
    write(wf / "version-bump.yml", (TPL / "workflows" / f"version-bump-{a.stack}.yml").read_text(), a.force, written)
    write(repo / "CHANGELOG.md", "# Changelog\n\n[Keep a Changelog](https://keepachangelog.com), [SemVer](https://semver.org). "
          "One `## <version>` entry per version, matching pyproject.toml / package.json.\n\n## 0.1.0\n### Added\n- Initial version.\n",
          a.force, written)
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
