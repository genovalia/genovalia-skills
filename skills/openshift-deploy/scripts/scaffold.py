#!/usr/bin/env python3
"""Render the openshift-deploy templates into a repo.

    scaffold.py <repo-dir> --app NAME --stack python|node --port 8000 \
        [--health /health] [--host-prod HOST] [--host-dev HOST] \
        [--config KEY,KEY?,...] [--secrets KEY,KEY?,...] [--sentry] [--migrate] [--force]

A key ending in "?" is optional: the Deploy workflow only sends it when the
GitHub Environment value is non-empty, so the app's own default stays in force.
Every other key must be set in both GitHub Environments or the deploy fails.

Never overwrites an existing file unless --force; prints what it wrote and
the bootstrap commands that come next. Standard library only.
"""
import argparse
import re
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
TPL = SKILL / "templates"
ENVS = {
    "dev": {"ns": "ul-val-genovalia-dv", "suffix": "-dev"},
    "prod": {"ns": "ul-val-genovalia-pr", "suffix": ""},
}
OC_FILES = [
    "deployment.yaml",
    "service.yaml",
    "route.yaml",
    "service-account.yaml",
    "role.yaml",
    "role-binding.yaml",
]
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
RESERVED = {"OPENSHIFT_CLUSTER", "OPENSHIFT_NAMESPACE", "OPENSHIFT_TOKEN", "SLACK_BOT_TOKEN", "SENTRY_ENVIRONMENT"}


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


def sync_block(keys: list[tuple[str, bool]], source: str) -> dict[str, str]:
    req = [k for k, opt in keys if not opt]
    opt = [k for k, opt in keys if opt]
    return {
        "ENV": "\n".join(f"          {k}: ${{{{ {source}.{k} }}}}" for k, _ in keys),
        "REQUIRED": " ".join(req) or "",
        "ARGS": "\n".join(f'            --from-literal={k}="${k}"' for k in req),
        "OPTIONAL": "\n".join(
            f'          if [ -n "${k}" ]; then args+=(--from-literal={k}="${k}"); fi' for k in opt
        ),
    }


def render_deploy_yml(app: str, config, secrets) -> str:
    text = (TPL / "workflows" / "deploy.yml").read_text()
    text = blocks(text, {"secrets": bool(secrets)})
    for prefix, keys, source in (("CONFIG", config, "vars"), ("SECRET", secrets, "secrets")):
        for part, value in sync_block(keys, source).items():
            marker = f"@@{prefix}_{part}@@"
            if part == "OPTIONAL" and not value:
                # drop the marker line and the comment that introduces it
                text = re.sub(rf"^ *# Optional keys:.*\n(?: *#.*\n)*(?= *{marker})", "", text, flags=re.M)
                text = re.sub(rf"^ *{marker}\n", "", text, flags=re.M)
            else:
                text = text.replace(f"{marker}", value)
    # a key list with only optional entries: no required loop
    text = re.sub(r"^ *missing=0\n *for k in ; do\n.*?\n *done\n *\[ \"\$missing\" = 0 \]\n", "", text, flags=re.M | re.S)
    # no required keys at all: `args=(\n          )` is valid bash, keep it
    return text.replace("__APP__", app)


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
    ap.add_argument("--host-prod", help="default <app>.apps.genovalia.ulaval.ca")
    ap.add_argument("--host-dev", help="default <app>-dev.apps.genovalia.ulaval.ca")
    ap.add_argument("--config", default="", help="ConfigMap keys (GitHub vars)")
    ap.add_argument("--secrets", default="", help="Secret keys (GitHub secrets)")
    ap.add_argument("--sentry", action="store_true", help="pin SENTRY_ENVIRONMENT in the manifests")
    ap.add_argument("--migrate", action="store_true", help="python: add a test job running alembic migrations on a real Postgres")
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    a = ap.parse_args()

    if not re.fullmatch(r"[a-z][a-z0-9-]*[a-z0-9]", a.app) or a.app.endswith("-dev"):
        sys.exit("--app must be kebab-case and must not end in -dev")
    config = parse_keys(a.config, "--config")
    secrets = parse_keys(a.secrets, "--secrets")
    if a.migrate and a.stack != "python":
        sys.exit("--migrate is only for --stack python (alembic)")
    if not config:
        sys.exit("--config needs at least one key (the Deployment always reads its ConfigMap)")
    if set(k for k, _ in config) & set(k for k, _ in secrets):
        sys.exit("a key cannot be both in --config and --secrets")
    hosts = {
        "prod": a.host_prod or f"{a.app}.apps.genovalia.ulaval.ca",
        "dev": a.host_dev or f"{a.app}-dev.apps.genovalia.ulaval.ca",
    }

    repo: Path = a.repo
    written: list[Path] = []
    keep = {"secrets": bool(secrets), "sentry": a.sentry}

    for env, e in ENVS.items():
        subs = {
            "__APP__": a.app,
            "__NAME__": a.app + e["suffix"],
            "__NS__": e["ns"],
            "__ENV__": env,
            "__PORT__": str(a.port),
            "__HEALTH__": a.health,
            "__HOST__": hosts[env],
        }
        for f in OC_FILES:
            text = blocks((TPL / "oc" / f).read_text(), keep)
            for k, v in subs.items():
                text = text.replace(k, v)
            write(repo / "oc" / env / f, text, a.force, written)

    write(repo / "deploy.py", (TPL / "deploy.py").read_text().replace("__APP__", a.app), a.force, written)
    wf = repo / ".github" / "workflows"
    write(wf / "deploy.yml", render_deploy_yml(a.app, config, secrets), a.force, written)
    write(wf / "restrict-main-source.yml", (TPL / "workflows" / "restrict-main-source.yml").read_text(), a.force, written)
    write(wf / "version-bump.yml", (TPL / "workflows" / f"version-bump-{a.stack}.yml").read_text(), a.force, written)
    test = blocks((TPL / "workflows" / f"test-{a.stack}.yml").read_text(), {"migrate": a.migrate, "nomigrate": not a.migrate})
    write(wf / "test.yml", test.replace("__APP__", a.app), a.force, written)

    leftovers = [p for p in written if re.search(r"__[A-Z]+__|@@[A-Z_]+@@|^# (>>>|<<<) ", p.read_text(), re.M)]
    if leftovers:
        sys.exit(f"BUG: unreplaced placeholders in {', '.join(map(str, leftovers))}")

    print("\nwritten:")
    for p in written:
        print(f"  {p}")
    print(
        "\nnext: review the files (resources, strategy, probes, volumes), then follow"
        f"\n{SKILL / 'references' / 'bootstrap.md'}"
        f"\nwith APP={a.app}"
    )


if __name__ == "__main__":
    main()
