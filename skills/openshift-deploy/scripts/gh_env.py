#!/usr/bin/env python3
"""Set a GitHub Environment's vars and secrets from a local KEY=value file.

    gh_env.py <repo-dir> --env dev|prod --file dev.env [--gh-repo OWNER/NAME] [--apply]

Each key goes where .github/workflows/deploy.yml reads it: `secrets.KEY` ->
`gh secret set --env`, `vars.KEY` -> `gh variable set --env`. A key that
deploy.yml does not read is refused, so nothing lands in GitHub that no
workflow uses, and a secret can never end up as a plain-text variable.
OPENSHIFT_CLUSTER / OPENSHIFT_NAMESPACE (vars) and OPENSHIFT_TOKEN (secret)
are accepted too; the namespace must match the standard for that env.

Dry run by default: prints the plan with secret values masked. --apply
writes. Secret values are passed on stdin, never on the command line.
The file stays outside git (put it in your scratch dir, delete it after).
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

NAMESPACES = {"dev": "ul-val-genovalia-dv", "prod": "ul-val-genovalia-pr"}
CLUSTER = "https://api.ul-pca-pr-ul01.ulaval.ca:6443"


def read_env_file(path: Path) -> dict[str, str]:
    values = {}
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            sys.exit(f"{path}:{n}: expected KEY=value")
        k, v = line.split("=", 1)
        k = k.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        values[k] = v
    return values


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", type=Path)
    ap.add_argument("--env", required=True, choices=sorted(NAMESPACES))
    ap.add_argument("--file", required=True, type=Path)
    ap.add_argument("--gh-repo", help="OWNER/NAME, default: from the origin remote")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    wf = (a.repo / ".github" / "workflows" / "deploy.yml").read_text()
    vars_ = set(re.findall(r"\bvars\.([A-Z0-9_]+)", wf))
    secrets = set(re.findall(r"\bsecrets\.([A-Z0-9_]+)", wf)) - {"SLACK_BOT_TOKEN", "GITHUB_TOKEN"}
    optional = set(re.findall(r'\[ -n "\$([A-Z0-9_]+)" \]', wf))

    gh_repo = a.gh_repo
    if not gh_repo:
        out = subprocess.run(["git", "-C", str(a.repo), "remote", "get-url", "origin"], capture_output=True, text=True).stdout
        m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", out.strip())
        if not m:
            sys.exit("cannot read the origin remote, pass --gh-repo")
        gh_repo = m.group(1)

    values = read_env_file(a.file)
    values.setdefault("OPENSHIFT_CLUSTER", CLUSTER)
    values.setdefault("OPENSHIFT_NAMESPACE", NAMESPACES[a.env])
    if values["OPENSHIFT_NAMESPACE"] != NAMESPACES[a.env]:
        sys.exit(f"OPENSHIFT_NAMESPACE must be {NAMESPACES[a.env]} for {a.env}")

    unknown = sorted(set(values) - vars_ - secrets)
    if unknown:
        sys.exit(f"not read by deploy.yml, refusing: {', '.join(unknown)}")
    empty = sorted(k for k, v in values.items() if v == "" and k not in optional)
    if empty:
        sys.exit(f"empty value for required key(s): {', '.join(empty)}")
    missing = sorted((vars_ | secrets) - set(values) - optional)

    print(f"{gh_repo} environment {a.env}{'' if a.apply else '  (dry run, --apply to write)'}")
    for k in sorted(values):
        if values[k] == "":
            print(f"  skip   {k} (optional, empty)")
            continue
        if k in secrets:
            print(f"  secret {k} = ****** ({len(values[k])} chars)")
            cmd, stdin = ["gh", "secret", "set", k, "--env", a.env, "--repo", gh_repo], values[k]
        else:
            print(f"  var    {k} = {values[k]}")
            cmd, stdin = ["gh", "variable", "set", k, "--env", a.env, "--repo", gh_repo, "--body", values[k]], None
        if a.apply:
            subprocess.run(cmd, input=stdin, text=True, check=True, capture_output=True)
    if missing:
        print(f"  not in the file (left as they are in GitHub): {', '.join(missing)}")


if __name__ == "__main__":
    main()
