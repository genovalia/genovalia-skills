#!/usr/bin/env python3
"""Build, tag, push and apply __APP__ to OpenShift.

Usage: python deploy.py <target> [--no-build] [--no-push] [--no-apply]
       [--token TOKEN --non-interactive]  (for CI)
       python deploy.py --list-targets <dev|prod>   (JSON list, for the CI matrix)
       python deploy.py --print-version

Standard genovalia-skills/openshift-deploy: only APP and TENANTS differ
between repos.
"""
import argparse
import json
import logging
import os
import subprocess
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

APP = "__APP__"
# Empty: one deployment per stage, plain manifests in oc/<stage>/, targets
# "dev" and "prod". Several tenants (same app deployed for several clients):
# Kustomize overlays in oc/overlays/<tenant>/<stage>/, targets "<tenant>-<stage>".
TENANTS: list[str] = __TENANTS__

SERVER = "api.ul-pca-pr-ul01.ulaval.ca:6443"
REGISTRY = "registre.apps.ul-pca-pr-ul01.ulaval.ca"

# dev and prod live in separate OpenShift namespaces.
NAMESPACES = {"dev": "ul-val-genovalia-dv", "prod": "ul-val-genovalia-pr"}


def build_targets() -> dict[str, dict]:
    """Deploy target -> stage, namespace, manifest dir and resource name.

    `name` is the Deployment, ConfigMap and image name. Plain layout: Service
    <name>-service, Route <name>-route, Secret <name>-secrets. Kustomize adds
    the prefix/suffix around each base name instead: <tenant>-<app>-service[-dev].
    """
    targets = {}
    for stage, namespace in NAMESPACES.items():
        suffix = "-dev" if stage == "dev" else ""
        if not TENANTS:
            targets[stage] = {
                "stage": stage,
                "namespace": namespace,
                "name": f"{APP}{suffix}",
                "manifest": f"oc/{stage}",
                "kustomize": False,
            }
        for tenant in TENANTS:
            targets[f"{tenant}-{stage}"] = {
                "stage": stage,
                "namespace": namespace,
                "name": f"{tenant}-{APP}{suffix}",
                "manifest": f"oc/overlays/{tenant}/{stage}",
                "kustomize": True,
            }
    return targets


TARGETS = build_targets()

# Bootstrap-only: applied once by hand with elevated credentials (see the
# openshift-deploy skill), not by this script. In the Kustomize layout they
# live apart, in oc/rbac/<stage>/, and the overlays never include them. The CI service account's Role
# has nothing on serviceaccounts/roles/rolebindings, so applying these here
# would 403 and abort every deploy.
BOOTSTRAP_ONLY_FILES = {"service-account.yaml", "role.yaml", "role-binding.yaml"}


def get_version() -> str:
    if Path("pyproject.toml").exists():
        for line in Path("pyproject.toml").read_text().splitlines():
            line = line.strip()
            if line.startswith("version ="):
                return line.split("=", 1)[1].strip().strip('"')
        raise RuntimeError("Could not find version in pyproject.toml")
    return json.loads(Path("package.json").read_text())["version"]


def run(cmd: list[str], input: str | None = None) -> None:
    logger.info("$ %s", " ".join(cmd))
    subprocess.run(cmd, check=True, input=input, text=input is not None or None)


def is_logged_in_to_oc() -> bool:
    result = subprocess.run(
        ["oc", "whoami"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return result.returncode == 0


def login_to_oc(
    user: str | None,
    password: str | None,
    token: str | None,
    force: bool,
    non_interactive: bool,
) -> None:
    if not force and is_logged_in_to_oc():
        logger.info(
            "==> Already logged in to OpenShift as %s",
            subprocess.check_output(["oc", "whoami"]).decode().strip(),
        )
        return

    if token:
        run(["oc", "login", f"--token={token}", f"--server={SERVER}"])
        return

    if non_interactive:
        raise SystemExit(
            "Non-interactive mode requires --token (or an existing oc login session)."
        )

    cmd = ["oc", "login", SERVER]
    if user:
        cmd += ["-u", user]
    if password:
        cmd += ["--password", password]
    # otherwise `oc login` prompts for credentials interactively
    run(cmd)


def build(image: str, version: str) -> None:
    run(["docker", "build", "-t", f"{image}:latest", "."])
    run(["docker", "tag", f"{image}:latest", f"{image}:{version}"])


def push(image: str, version: str) -> None:
    token = subprocess.check_output(["oc", "whoami", "--show-token"]).decode().strip()
    # Username is ignored by the registry (only the token is checked), and must
    # not be `oc whoami` here: for a service account that's `system:serviceaccount:ns:name`,
    # and HTTP Basic Auth splits "user:pass" on the *first* colon, corrupting both
    # fields when the username itself contains one.
    run(["docker", "login", "-u", "unused", "-p", token, REGISTRY])
    run(["docker", "push", f"{image}:{version}"])
    run(["docker", "push", f"{image}:latest"])


def manifest_files(manifest: str) -> list[str]:
    files = sorted(
        str(p)
        for p in Path(manifest).glob("*.yaml")
        if p.name not in BOOTSTRAP_ONLY_FILES
    )
    if not files:
        raise RuntimeError(f"No manifest files found in {manifest}/")
    return files


def apply_and_rollout(target: dict, version: str) -> None:
    namespace, name = target["namespace"], target["name"]
    label = f"app.kubernetes.io/version={version}"
    if target["kustomize"]:
        # Rendered once and fed to both apply and label, so both see the
        # same objects (and `oc label -k` support varies across versions).
        logger.info("$ oc kustomize %s", target["manifest"])
        rendered = subprocess.run(
            ["oc", "kustomize", target["manifest"]], check=True, capture_output=True, text=True
        ).stdout
        run(["oc", "apply", "-n", namespace, "-f", "-"], input=rendered)
        run(["oc", "label", "--overwrite", "-n", namespace, "-f", "-", label], input=rendered)
    else:
        file_args = [arg for f in manifest_files(target["manifest"]) for arg in ("-f", f)]
        run(["oc", "apply", "-n", namespace, *file_args])
        # Tag every applied object (Deployment, Service, Route, ...) with the deployed version.
        run(["oc", "label", "--overwrite", "-n", namespace, *file_args, label])
    # Unconditional restart: `:latest` did not change name, and the ConfigMap/
    # Secret synced just before this step are only read at pod start.
    run(["oc", "rollout", "restart", "-n", namespace, f"deployment/{name}"])
    run(["oc", "rollout", "status", "-n", namespace, f"deployment/{name}"])


def write_github_output(env: str, version: str, deployment: str) -> None:
    # Lets the CI workflow read these back as step outputs (e.g. for a Slack
    # notification) without duplicating the TARGETS mapping in YAML.
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    with open(output_file, "a") as f:
        f.write(f"environment={env}\n")
        f.write(f"version={version}\n")
        f.write(f"deployment={deployment}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Build, push and apply {APP} to OpenShift"
    )
    parser.add_argument("target", nargs="?", choices=sorted(TARGETS))
    parser.add_argument(
        "--list-targets",
        choices=sorted(NAMESPACES),
        help="print the targets of a stage as a JSON list and exit",
    )
    parser.add_argument("--print-version", action="store_true", help="print the version and exit")
    parser.add_argument("--no-build", action="store_true", help="skip build step")
    parser.add_argument("--no-push", action="store_true", help="skip push step")
    parser.add_argument(
        "--no-apply", action="store_true", help="skip apply/rollout step"
    )
    parser.add_argument("-u", "--user", default=None, help="OpenShift username")
    parser.add_argument("-p", "--password", default=None, help="OpenShift password")
    parser.add_argument(
        "-t",
        "--token",
        default=None,
        help="OpenShift service account token, for non-interactive login "
        "(oc login --token=...). Takes precedence over --user/--password.",
    )
    parser.add_argument(
        "-f",
        "--force-login",
        action="store_true",
        help="force (re-)login even if already logged in to OpenShift",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt (e.g. in CI): fail immediately instead of asking for credentials",
    )
    args = parser.parse_args()

    if args.list_targets:
        print(json.dumps([t for t, c in TARGETS.items() if c["stage"] == args.list_targets]))
        return
    if args.print_version:
        print(get_version())
        return
    if not args.target:
        parser.error("a target is required")

    target = TARGETS[args.target]
    image = f"{REGISTRY}/{target['namespace']}/{target['name']}"
    version = get_version()

    if not args.no_build:
        build(image, version)

    if not args.no_push or not args.no_apply:
        login_to_oc(
            args.user, args.password, args.token, args.force_login, args.non_interactive
        )

    if not args.no_push:
        push(image, version)

    if not args.no_apply:
        apply_and_rollout(target, version)

    write_github_output(args.target, version, target["name"])
    logger.info("Done.")


if __name__ == "__main__":
    main()
