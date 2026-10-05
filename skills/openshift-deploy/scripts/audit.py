#!/usr/bin/env python3
"""Audit a repo against the openshift-deploy standard.

    audit.py <repo-dir> [--app NAME] [--gh-repo OWNER/NAME] [--no-github]

Checks the oc/<env>/ manifests (names, namespaces, image, envFrom, RBAC),
deploy.py, the workflows, and - through `gh api`, read-only - the GitHub
Environments: OpenShift access per environment, and the vars/secrets that
deploy.yml reads versus the ones actually set (missing = deploy breaks,
unused = drift to delete).

Prints ERROR / WARN / OK lines; exit code 1 when there is at least one ERROR.
Standard library only: the manifest parsing is regex-based and tailored to the
indentation of our own files, not a general YAML parser.
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from scaffold import names_for

SKILL = Path(__file__).resolve().parent.parent
TPL = SKILL / "templates"
REGISTRY = "registre.apps.ul-pca-pr-ul01.ulaval.ca"
ENVS = {
    "dev": {"ns": "ul-val-genovalia-dv", "suffix": "-dev"},
    "prod": {"ns": "ul-val-genovalia-pr", "suffix": ""},
}
REQUIRED_FILES = ["deployment.yaml", "service.yaml", "route.yaml", "service-account.yaml", "role.yaml", "role-binding.yaml"]
BOOTSTRAP_FILES = {"service-account.yaml", "role.yaml", "role-binding.yaml"}
# Tokens come from `oc create token --duration=8760h`: they expire a year after
# being issued and nothing renews them. The GitHub secret's updated_at is the
# closest record of the issue date.
TOKEN_LIFETIME = timedelta(days=365)
TOKEN_WARN_BEFORE = timedelta(days=30)
INFRA_VARS = {"OPENSHIFT_CLUSTER", "OPENSHIFT_NAMESPACE"}
INFRA_SECRETS = {"OPENSHIFT_TOKEN", "SLACK_BOT_TOKEN", "GITHUB_TOKEN"}

results: list[tuple[str, str, str]] = []


def report(level: str, section: str, msg: str) -> None:
    results.append((level, section, msg))


# ---------------------------------------------------------------- manifests

def block(text: str, key: str) -> str:
    """Lines indented under the first `key:` (any indentation), as text."""
    m = re.search(rf"^(\s*){key}:\s*\n", text, re.M)
    if not m:
        return ""
    indent = len(m.group(1))
    out = []
    for line in text[m.end():].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        out.append(line)
    return "\n".join(out)


def first(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.M)
    return m.group(1).strip("\"'") if m else None


def meta(text: str, field: str) -> str | None:
    md = block(text, "metadata")
    lines = [ln for ln in md.splitlines() if ln.strip()]
    if not lines:
        return None
    base = min(len(ln) - len(ln.lstrip()) for ln in lines)
    for ln in lines:
        if len(ln) - len(ln.lstrip()) == base and ln.strip().startswith(f"{field}:"):
            return ln.split(":", 1)[1].strip().strip("\"'")
    return None


def yaml_list(text: str, key: str) -> list[str]:
    m = re.search(rf"^\s*-?\s*{key}:\s*\[(.*?)\]", text, re.M)
    if m:
        return [s.strip().strip("\"'") for s in m.group(1).split(",") if s.strip()]
    # block list: "- item" lines right under the key, at its indentation or deeper
    m = re.search(rf"^(\s*)-?\s*{key}:\s*\n", text, re.M)
    if not m:
        return []
    items = []
    for ln in text[m.end():].splitlines():
        s = ln.strip()
        if s.startswith("#") or not s:
            continue
        if not s.startswith("-") or len(ln) - len(ln.lstrip()) < len(m.group(1)):
            break
        items.append(s[1:].strip().strip("\"'"))
    return items


def role_rules(text: str) -> list[dict]:
    rules = []
    for chunk in re.split(r"^\s*- apiGroups:", text, flags=re.M)[1:]:
        chunk = "apiGroups:" + chunk
        chunk = re.sub(r"^\s*#.*\n", "", chunk, flags=re.M)
        rules.append({
            "resources": set(yaml_list(chunk, "resources")),
            "verbs": set(yaml_list(chunk, "verbs")),
            "names": set(yaml_list(chunk, "resourceNames")),
        })
    return rules


def check_role(sec: str, role: str, objects: dict[str, str], has_host: bool) -> None:
    rules = role_rules(role)
    for resource, name in objects.items():
        scoped = any(resource in r["resources"] and name in r["names"] and {"update", "patch"} <= r["verbs"] for r in rules)
        create = any(resource in r["resources"] and not r["names"] and "create" in r["verbs"] for r in rules)
        if not scoped:
            report("ERROR", sec, f"role.yaml: no get/update/patch on {resource} scoped to {name}")
        if not create:
            report("ERROR", sec, f"role.yaml: no unscoped 'create' rule on {resource} (create cannot be scoped by resourceNames)")
    if has_host and not any("routes/custom-host" in r["resources"] and "create" in r["verbs"] for r in rules):
        report("ERROR", sec, "role.yaml: Route sets spec.host but Role lacks routes/custom-host create")
    if any("routes/custom-host" in r["resources"] and "update" in r["verbs"] for r in rules):
        report("ERROR", sec, "role.yaml: 'update' on routes/custom-host is self-escalation, the Role will be refused")
    if not any("imagestreams/layers" in r["resources"] for r in rules):
        report("ERROR", sec, "role.yaml: no imagestreams/layers rule, docker push will be denied")


def audit_env(repo: Path, app: str, env: str) -> None:
    """Plain layout: oc/<env>/ files."""
    sec = f"oc/{env}"
    ns = ENVS[env]["ns"]
    n = names_for(app, None, env)
    d = repo / "oc" / env
    if not d.is_dir():
        report("ERROR", sec, f"missing directory {d}")
        return
    files = {p.name: p.read_text() for p in d.glob("*.y*ml")}
    for f in REQUIRED_FILES:
        if f not in files:
            report("ERROR", sec, f"missing {f}")
    if "ci-token.yaml" in files:
        report("WARN", sec, "ci-token.yaml: a never-expiring token Secret; issue it with `oc create token --duration=8760h` instead and delete the Secret")
    for f in sorted(files):
        if f.endswith(".yml"):
            report("WARN", sec, f"{f}: use the .yaml extension (deploy.py only applies *.yaml)")
        elif f not in BOOTSTRAP_FILES and f not in REQUIRED_FILES:
            report("WARN", sec, f"{f}: extra manifest applied by deploy.py, check role.yaml covers its kind")
        txt = files[f]
        mns = meta(txt, "namespace")
        if mns and mns != ns:
            report("ERROR", sec, f"{f}: namespace {mns}, expected {ns}")

    sa_name = f"github-ci-{app}"
    for f in ("service-account.yaml", "role.yaml", "role-binding.yaml"):
        if f in files and meta(files[f], "name") != sa_name:
            report("ERROR", sec, f"{f}: name {meta(files[f], 'name')}, expected {sa_name}")
    check_objects(sec, env, ns, n, files.get("deployment.yaml", ""), files.get("service.yaml", ""),
                  files.get("route.yaml", ""), files.get("role.yaml"))
    if not [r for r in results if r[1] == sec and r[0] == "ERROR"]:
        report("OK", sec, f"{n['name']} in {ns}")


def check_objects(sec: str, env: str, ns: str, n: dict, dep: str, svc: str, route: str,
                  role: str | None, configmap: str = "") -> None:
    """Names, labels, image, envFrom and RBAC of one target's objects."""
    name = n["name"]
    secret_refs: list[str] = []
    if dep:
        kind = first(r"^kind:\s*(\S+)", dep)
        if kind != "Deployment":
            report("ERROR", sec, f"deployment.yaml: kind {kind}, expected Deployment (DeploymentConfig is deprecated)")
        if meta(dep, "name") != name:
            report("ERROR", sec, f"Deployment name {meta(dep, 'name')}, expected {name}")
        mns = meta(dep, "namespace")
        if mns and mns != ns:
            report("ERROR", sec, f"Deployment namespace {mns}, expected {ns}")
        if first(r"^\s+environment:\s*(\S+)", block(dep, "metadata")) != env:
            report("WARN", sec, f"Deployment metadata.labels.environment should be {env}")
        if first(r"^\s+app:\s*(\S+)", block(dep, "matchLabels")) != name:
            report("ERROR", sec, f"Deployment selector app should be {name}")
        images = re.findall(r"^\s*image:\s*(\S+)", dep, re.M)
        own = f"{REGISTRY}/{ns}/{name}:latest"
        if own not in images:
            report("ERROR", sec, f"no container uses image {own} (found: {', '.join(images) or 'none'})")
        cms = set(re.findall(r"configMapRef:\s*\n\s+name:\s*(\S+)", dep))
        secret_refs = sorted(set(re.findall(r"secretRef:\s*\n\s+name:\s*(\S+)", dep)))
        if cms != {name}:
            report("ERROR", sec, f"envFrom configMapRef {sorted(cms)}, expected ['{name}']")
        if secret_refs and secret_refs != [n["secret"]]:
            report("ERROR", sec, f"envFrom secretRef {secret_refs}, expected ['{n['secret']}']")
        sentry = re.findall(r"name:\s*SENTRY_ENVIRONMENT\s*\n\s*value:\s*\"?(\w+)", dep)
        sentry += re.findall(r"^\s+SENTRY_ENVIRONMENT:\s*\"?(\w+)", configmap, re.M)
        if any(v != env for v in sentry):
            report("ERROR", sec, f"SENTRY_ENVIRONMENT pinned to {set(sentry)}, expected {env}")

    if svc:
        if meta(svc, "name") != n["service"]:
            report("ERROR", sec, f"Service name {meta(svc, 'name')}, expected {n['service']}")
        if first(r"^\s+app:\s*(\S+)", block(svc, "selector")) != name:
            report("ERROR", sec, f"Service selector app should be {name}")

    host = None
    if route:
        if meta(route, "name") != n["route"]:
            report("ERROR", sec, f"Route name {meta(route, 'name')}, expected {n['route']}")
        to = first(r"^\s+name:\s*(\S+)", block(route, "to"))
        if to != n["service"]:
            report("ERROR", sec, f"Route points to {to}, expected {n['service']}")
        host = first(r"^\s+host:\s*(\S+)", route)
        if host and host.endswith(".apps.genovalia.ulaval.ca") and env == "dev" and "-dev" not in host:
            report("WARN", sec, f"dev Route host {host} has no -dev in it")

    if role:
        objects = {
            "deployments": name,
            "services": n["service"],
            "routes": n["route"],
            "configmaps": name,
            "imagestreams": name,
        }
        if secret_refs:
            objects["secrets"] = n["secret"]
        check_role(sec, role, objects, bool(host))


def render(repo: Path, overlay: str) -> str | None:
    """`oc kustomize` (or kubectl) of an overlay, on a copy of oc/ with empty
    placeholders for the env files the Deploy workflow would write."""
    tool = shutil.which("oc") or shutil.which("kubectl")
    if not tool:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(repo / "oc", Path(tmp) / "oc")
        leaf = Path(tmp) / overlay
        for f in ("config.env", "secret.env"):
            (leaf / f).touch(exist_ok=True)
        r = subprocess.run([tool, "kustomize", str(leaf)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "kustomize failed")
        return r.stdout


def audit_kustomize(repo: Path, app: str, targets: list[dict]) -> None:
    """Kustomize layout: oc/base, oc/overlays/<tenant>/<stage>, oc/rbac/<stage>."""
    sec = "oc"
    base = repo / "oc" / "base" / "kustomization.yaml"
    if not base.exists():
        report("ERROR", sec, "missing oc/base/kustomization.yaml")
    elif "nameref" not in base.read_text():
        report("ERROR", sec, "base declares no nameReference for Route spec.to.name: a rename leaves the Route dangling")
    gi = (repo / ".gitignore").read_text() if (repo / ".gitignore").exists() else ""
    if "config.env" not in gi or "secret.env" not in gi:
        report("ERROR", sec, ".gitignore must exclude oc/overlays/*/*/config.env and secret.env (they hold secret values)")
    for stage in ENVS:
        d = repo / "oc" / "rbac" / stage
        for f in REQUIRED_FILES[3:]:
            if not (d / f).exists():
                report("ERROR", f"oc/rbac/{stage}", f"missing {f}")
            elif meta((d / f).read_text(), "name") != f"github-ci-{app}":
                report("ERROR", f"oc/rbac/{stage}", f"{f}: name should be github-ci-{app}")
            elif meta((d / f).read_text(), "namespace") != ENVS[stage]["ns"]:
                report("ERROR", f"oc/rbac/{stage}", f"{f}: namespace should be {ENVS[stage]['ns']}")
    if not (repo / "oc" / "base").is_dir():
        return
    for tg in targets:
        sec = f"oc/overlays/{tg['tenant']}/{tg['stage']}"
        if not (repo / sec / "kustomization.yaml").exists():
            report("ERROR", sec, "missing kustomization.yaml")
            continue
        try:
            out = render(repo, sec)
        except RuntimeError as exc:
            report("ERROR", sec, f"kustomize build fails: {exc}")
            continue
        if out is None:
            report("WARN", sec, "neither oc nor kubectl on PATH: rendered objects not checked")
            continue
        objs: dict[str, str] = {}
        for doc in re.split(r"^---\s*$", out, flags=re.M):
            kind = first(r"^kind:\s*(\S+)", doc)
            if kind:
                objs.setdefault(kind, doc)
        role_file = repo / "oc" / "rbac" / tg["stage"] / "role.yaml"
        n = tg["names"]
        cm = objs.get("ConfigMap", "")
        if objs.get("Deployment") and not meta(objs["Deployment"], "namespace"):
            report("ERROR", sec, f"overlay sets no namespace (expected {tg['ns']}): generated names are matched per namespace")
        if cm and meta(cm, "name") != n["name"]:
            report("ERROR", sec, f"ConfigMap name {meta(cm, 'name')}, expected {n['name']}")
        check_objects(sec, tg["stage"], tg["ns"], n, objs.get("Deployment", ""), objs.get("Service", ""),
                      objs.get("Route", ""), role_file.read_text() if role_file.exists() else None, cm)
        if not [r for r in results if r[1] == sec and r[0] == "ERROR"]:
            report("OK", sec, f"{tg['target']}: {n['name']} in {tg['ns']}")


# ---------------------------------------------------------------- deploy.py / workflows

def normalise(text: str) -> list[str]:
    return [ln.rstrip() for ln in text.strip().splitlines()]


def audit_files(repo: Path, app: str, kustomize: bool) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
    sec = "deploy.py"
    dp = repo / "deploy.py"
    if not dp.exists():
        report("ERROR", sec, "missing deploy.py")
    else:
        mine = normalise(dp.read_text())
        m = re.search(r"^TENANTS: list\[str\] = (.*)$", dp.read_text(), re.M)
        tpl = normalise((TPL / "deploy.py").read_text().replace("__APP__", app)
                        .replace("__TENANTS__", m.group(1) if m else "[]"))
        if mine == tpl:
            report("OK", sec, "identical to the template")
        else:
            diff = len(set(mine) ^ set(tpl))
            report("WARN", sec, f"differs from the template (~{diff} lines): run `diff {TPL / 'deploy.py'} {dp}` and justify or realign")
            text = dp.read_text()
            for env, e in ENVS.items():
                if e["ns"] not in text:
                    report("ERROR", sec, f"namespace {e['ns']} ({env}) not found: dev and prod must use separate namespaces")
            if "BOOTSTRAP_ONLY_FILES" not in text and (repo / "oc" / "dev" / "role.yaml").exists():
                report("ERROR", sec, "applies oc/<env>/ wholesale: the RBAC files there will 403 for the CI account")

    sec = "workflows"
    wf = repo / ".github" / "workflows"
    deploy = wf / "deploy.yml"
    if not deploy.exists():
        report("ERROR", sec, "missing .github/workflows/deploy.yml")
    restrict = wf / "restrict-main-source.yml"
    if not restrict.exists():
        report("ERROR", sec, "missing restrict-main-source.yml")
    elif normalise(restrict.read_text()) != normalise((TPL / "workflows" / "restrict-main-source.yml").read_text()):
        report("WARN", sec, "restrict-main-source.yml differs from the template")
    if not (wf / "version-bump.yml").exists():
        report("ERROR", sec, "missing version-bump.yml")
    if not any((wf / f).exists() for f in ("test.yml", "ci.yml")):
        report("ERROR", sec, "missing test.yml (PR checks)")

    env_vars: set[str] = set()
    env_secrets: set[str] = set()
    optional: set[str] = set()
    all_vars: set[str] = set()
    all_secrets: set[str] = set()
    for f in wf.glob("*.y*ml") if wf.is_dir() else []:
        t = f.read_text()
        all_vars |= set(re.findall(r"\bvars\.([A-Z0-9_]+)", t))
        all_secrets |= set(re.findall(r"\bsecrets\.([A-Z0-9_]+)", t))
    if deploy.exists():
        t = deploy.read_text()
        env_vars = set(re.findall(r"\bvars\.([A-Z0-9_]+)", t))
        env_secrets = set(re.findall(r"\bsecrets\.([A-Z0-9_]+)", t))
        # keys only sent when non-empty: leaving them unset is allowed
        optional = set(re.findall(r'\[ -n "\$([A-Z0-9_]+)" \]', t))
        if "ul-git-pr-resul-recherche-runners" not in t:
            report("ERROR", sec, "deploy.yml does not run on the ul-git-pr-resul-recherche-runners group (cluster access)")
        tag_part = t.split("\n  tag:", 1)[-1].split("\n\n", 1)[0] if kustomize else t.split("Tag version", 1)[-1].split("\n\n", 1)[0]
        if "github.ref_name == 'main'" not in tag_part:
            report("WARN", sec, "release tagging not guarded by github.ref_name == 'main'")
        if kustomize and "fromJSON(needs.targets.outputs.targets)" not in t:
            report("WARN", sec, "deploy.yml predates the Kustomize template (no per-target matrix): realign when touched")
        if "SENTRY_ENVIRONMENT" in env_vars:
            report("WARN", sec, "SENTRY_ENVIRONMENT read from a GitHub var: pin it in the manifests")
        if not kustomize and "RESOURCE_NAME" not in t:
            report("WARN", sec, "deploy.yml predates the template (no DEPLOY_ENV/RESOURCE_NAME job env): realign when touched")
    if not [r for r in results if r[1] == sec and r[0] == "ERROR"]:
        report("OK", sec, "deploy, test, version-bump, restrict-main-source present")
    return env_vars - INFRA_VARS - optional, env_secrets - INFRA_SECRETS - optional, all_vars, all_secrets, optional


# ---------------------------------------------------------------- GitHub

def gh(path: str) -> dict | None:
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)


def names(path: str, key: str) -> set[str] | None:
    # --paginate: environment variables come 10 per page by default, 30 at most
    r = subprocess.run(["gh", "api", "--paginate", f"{path}?per_page=30", "--jq", f".{key}[].name"], capture_output=True, text=True)
    return None if r.returncode != 0 else set(r.stdout.split())


def check_token(sec: str, path: str) -> None:
    updated = (gh(path) or {}).get("updated_at")
    if not updated:
        return
    issued = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    expires = issued + TOKEN_LIFETIME
    now = datetime.now(timezone.utc)
    when = f"set {issued:%Y-%m-%d}, expires ~{expires:%Y-%m-%d} if issued for 8760h"
    if now >= expires:
        report("ERROR", sec, f"OPENSHIFT_TOKEN probably expired ({when}): rotate it")
    elif now >= expires - TOKEN_WARN_BEFORE:
        report("WARN", sec, f"OPENSHIFT_TOKEN expires soon ({when}): rotate it")
    else:
        report("OK", sec, f"OPENSHIFT_TOKEN {when}")


def audit_github(gh_repo: str, targets: list[dict], need_vars: set[str], need_secrets: set[str], all_vars: set[str], all_secrets: set[str]) -> None:
    sec = "github"
    envs = gh(f"repos/{gh_repo}/environments")
    if envs is None:
        report("ERROR", sec, f"cannot read environments of {gh_repo} (gh auth? admin rights needed for secrets)")
        return
    by_name = {e["name"]: e for e in envs.get("environments", [])}
    repo_vars = names(f"repos/{gh_repo}/actions/variables", "variables") or set()
    repo_secrets = names(f"repos/{gh_repo}/actions/secrets", "secrets") or set()
    org_secrets = names(f"repos/{gh_repo}/actions/organization-secrets", "secrets") or set()
    org_vars = names(f"repos/{gh_repo}/actions/organization-variables", "variables") or set()

    if "SLACK_BOT_TOKEN" not in repo_secrets | org_secrets:
        report("ERROR", sec, "SLACK_BOT_TOKEN not available (repo or org secret)")
    for v in sorted(repo_vars & INFRA_VARS):
        report("WARN", sec, f"repo-level var {v}: move it to each Environment (dev and prod are different namespaces)")
    if "OPENSHIFT_TOKEN" in repo_secrets:
        report("WARN", sec, "repo-level secret OPENSHIFT_TOKEN: move it to each Environment (one CI account per namespace)")
        check_token(sec, f"repos/{gh_repo}/actions/secrets/OPENSHIFT_TOKEN")
    for v in sorted(repo_vars - all_vars):
        report("WARN", sec, f"repo-level var {v} is read by no workflow: delete")
    for s in sorted(repo_secrets - all_secrets):
        report("WARN", sec, f"repo-level secret {s} is read by no workflow: delete")
    extra_envs = set(by_name) - {tg["target"] for tg in targets} - {"copilot", "github-pages"}
    if extra_envs:
        report("WARN", sec, f"extra environments {sorted(extra_envs)}: not covered by this audit")

    for tg in targets:
        env, stage = tg["target"], tg["stage"]
        s = f"github/{env}"
        if env not in by_name:
            report("ERROR", s, "environment missing")
            continue
        policy = by_name[env].get("deployment_branch_policy")
        if stage == "prod":
            if not policy:
                report("WARN", s, "no deployment branch policy: prod can be deployed from any branch (restrict to main)")
            elif policy.get("custom_branch_policies"):
                pol = gh(f"repos/{gh_repo}/environments/{env}/deployment-branch-policies") or {}
                if "main" not in {p["name"] for p in pol.get("branch_policies", [])}:
                    report("ERROR", s, "branch policy does not allow main")
        ev = names(f"repos/{gh_repo}/environments/{env}/variables", "variables")
        es = names(f"repos/{gh_repo}/environments/{env}/secrets", "secrets")
        if ev is None or es is None:
            report("ERROR", s, "cannot list variables/secrets")
            continue
        for v in sorted(INFRA_VARS - ev):
            report("ERROR", s, f"var {v} missing at environment level")
        if "OPENSHIFT_TOKEN" not in es:
            report("ERROR", s, "secret OPENSHIFT_TOKEN missing at environment level")
        else:
            check_token(s, f"repos/{gh_repo}/environments/{env}/secrets/OPENSHIFT_TOKEN")
        if "OPENSHIFT_NAMESPACE" in ev:
            ns = (gh(f"repos/{gh_repo}/environments/{env}/variables/OPENSHIFT_NAMESPACE") or {}).get("value")
            if ns != tg["ns"]:
                report("ERROR", s, f"OPENSHIFT_NAMESPACE={ns}, expected {tg['ns']}")
        for v in sorted(need_vars - ev - repo_vars - org_vars):
            report("ERROR", s, f"var {v} read by deploy.yml but not set")
        for v in sorted(need_secrets - es - repo_secrets - org_secrets):
            report("ERROR", s, f"secret {v} read by deploy.yml but not set")
        for v in sorted(ev - need_vars - INFRA_VARS - all_vars):
            report("WARN", s, f"var {v} set but read by no workflow: delete (drift)")
        for v in sorted(es - need_secrets - INFRA_SECRETS - all_secrets):
            report("WARN", s, f"secret {v} set but read by no workflow: delete (drift)")
        if "SENTRY_ENVIRONMENT" in ev:
            report("WARN", s, "var SENTRY_ENVIRONMENT: pinned in the manifests, delete it here")
        if not [r for r in results if r[1] == s and r[0] == "ERROR"]:
            report("OK", s, f"{len(ev)} vars, {len(es)} secrets")


# ---------------------------------------------------------------- main

def detect_targets(repo: Path, app: str) -> tuple[bool, list[dict]]:
    """(kustomize?, targets) from deploy.py TENANTS, else from oc/overlays/."""
    tenants: list[str] = []
    dp = repo / "deploy.py"
    if dp.exists():
        m = re.search(r"^TENANTS: list\[str\] = \[(.*)\]$", dp.read_text(), re.M)
        if m:
            tenants = [s.strip().strip("\"'") for s in m.group(1).split(",") if s.strip()]
    overlays = repo / "oc" / "overlays"
    if not tenants and overlays.is_dir():
        tenants = sorted(p.name for p in overlays.iterdir() if p.is_dir())
    kustomize = bool(tenants)
    targets = []
    for stage, e in ENVS.items():
        for tenant in tenants or [None]:
            targets.append({
                "target": f"{tenant}-{stage}" if tenant else stage,
                "tenant": tenant,
                "stage": stage,
                "ns": e["ns"],
                "names": names_for(app, tenant, stage),
            })
    return kustomize, targets


def detect_app(repo: Path) -> str | None:
    dp = repo / "deploy.py"
    if dp.exists():
        m = re.search(r'^APP\s*=\s*"([^"]+)"', dp.read_text(), re.M)
        if m:
            return m.group(1)
    dep = repo / "oc" / "prod" / "deployment.yaml"
    if dep.exists():
        return meta(dep.read_text(), "name")
    return None


def detect_gh_repo(repo: Path) -> str | None:
    r = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, text=True)
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", r.stdout.strip())
    return m.group(1) if m else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", type=Path)
    ap.add_argument("--app", help="default: APP in deploy.py, else oc/prod Deployment name")
    ap.add_argument("--gh-repo", help="OWNER/NAME, default: from the origin remote")
    ap.add_argument("--no-github", action="store_true")
    a = ap.parse_args()

    app = a.app or detect_app(a.repo)
    if not app:
        sys.exit("cannot detect the app name, pass --app")
    print(f"# audit {a.repo.resolve()} (app={app})\n")

    kustomize, targets = detect_targets(a.repo, app)
    if kustomize:
        print(f"Kustomize layout, targets: {', '.join(tg['target'] for tg in targets)}\n")
        audit_kustomize(a.repo, app, targets)
    else:
        for env in ENVS:
            audit_env(a.repo, app, env)
    need_vars, need_secrets, all_vars, all_secrets, _ = audit_files(a.repo, app, kustomize)
    if not a.no_github:
        gh_repo = a.gh_repo or detect_gh_repo(a.repo)
        if gh_repo:
            audit_github(gh_repo, targets, need_vars, need_secrets, all_vars, all_secrets)
        else:
            report("WARN", "github", "no GitHub origin remote, pass --gh-repo")

    order = {"ERROR": 0, "WARN": 1, "OK": 2}
    for level, sec, msg in sorted(results, key=lambda r: (r[1], order[r[0]])):
        print(f"{level:5} [{sec}] {msg}")
    n_err = sum(r[0] == "ERROR" for r in results)
    n_warn = sum(r[0] == "WARN" for r in results)
    print(f"\n{n_err} error(s), {n_warn} warning(s)")
    sys.exit(1 if n_err else 0)


if __name__ == "__main__":
    main()
