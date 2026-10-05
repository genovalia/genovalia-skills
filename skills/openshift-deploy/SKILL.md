---
name: openshift-deploy
description: Genovalia standard for deploying an app to the UL OpenShift cluster (ul-val-genovalia-dv / -pr) - oc/<env>/ manifests, CI service account RBAC, deploy.py, GitHub workflows (deploy, test, version-bump, restrict-main-source) and GitHub Environment vars/secrets synced into ConfigMap/Secret. Use when setting up deployment or CI/CD for a new Genovalia project, adding/removing a config variable or secret of a deployed app, auditing or realigning an existing repo's oc/ folder, workflows or GitHub environments, or debugging a failed Deploy run.
---

# OpenShift deploy standard (Genovalia)

One layout for every deployed repo, so a new project is a scaffold plus a bootstrap, and any repo can be audited against it. Reference implementations: `ovision-api` (backend) and `ovision` (frontend) are the closest; `genovine-*` and `data-explorer-*` follow the same workflow logic but still use one shared namespace (see Legacy).

Decided 2026-10-05: **dev and prod in separate namespaces**, **flat `oc/dev/` + `oc/prod/` manifests** (no Kustomize), **`<name>`, `<name>-service`, `<name>-route` naming**, **prod deployed automatically on merge to `main`** (the human gate is the required review of the `dev` -> `main` PR), **CI may create Routes with a custom host** (`routes/custom-host` create, no admin step per new Route).

Confluence ("Comptes de service GitHub CI pour auto-déploiement", "Versionnement, tests et releases", Jul-Aug 2026) predates these choices. Where it says otherwise (shared namespace, manual prod deploy, no Secret or custom-host access for CI, manual git tag), this skill is the newer rule. Its token procedure (`oc create token --duration=8760h`) is kept as is.

## The standard

```
deploy.py                       templates/deploy.py, only APP differs
oc/dev/  oc/prod/               one folder per env, *.yaml only
  deployment.yaml service.yaml route.yaml       applied by deploy.py
  service-account.yaml role.yaml role-binding.yaml   bootstrap-only (namespace admin, once)
.github/workflows/
  deploy.yml                    push dev -> dev, push main -> prod, manual dispatch
  test.yml                      PR checks (tests + docker build)
  version-bump.yml              PR must bump pyproject.toml / package.json version
  restrict-main-source.yml      PRs to main only from dev (identical everywhere)
```

| | dev | prod |
|---|---|---|
| Namespace | `ul-val-genovalia-dv` | `ul-val-genovalia-pr` |
| `<name>` (Deployment, ConfigMap, ImageStream, `app` label) | `<app>-dev` | `<app>` |
| Service / Route | `<name>-service` / `<name>-route` | same |
| Secret | `<name>-secrets` | same |
| Image | `registre.apps.ul-pca-pr-ul01.ulaval.ca/<ns>/<name>:latest` + `:<version>` | same |
| Host (default) | `<app>-dev.apps.genovalia.ulaval.ca` | `<app>.apps.genovalia.ulaval.ca` |
| CI account | `github-ci-<app>` (SA + Role + RoleBinding), one per namespace; token from `oc create token --duration=8760h`, rotated yearly | same |
| Git branch / GitHub Environment | `dev` / `dev` | `main` / `prod` |

GitHub layout:

| Where | What |
|---|---|
| Environment `dev`, `prod` - vars | `OPENSHIFT_CLUSTER`, `OPENSHIFT_NAMESPACE`, then every ConfigMap key |
| Environment `dev`, `prod` - secrets | `OPENSHIFT_TOKEN` (that namespace's CI token), then every Secret key |
| Repo (or org) secret | `SLACK_BOT_TOKEN` only |
| Environment `prod` | deployment branch policy: `main` only |
| Branch protection | `dev` and `main`: PR + required checks; `main`: 1 approving review |

GitHub is the source of truth for config values: `deploy.yml` re-creates the ConfigMap `<name>` and Secret `<name>-secrets` from the Environment on every deploy, then `deploy.py` builds, pushes, applies `oc/<env>/` (minus the bootstrap files), labels everything with `app.kubernetes.io/version` and restarts the Deployment. A prod deploy from `main` tags `v<version>`. Slack: `deployment-dev`, `deployment-prod`, failures in `github-ci`.

Never: a value in a committed manifest that belongs in GitHub, a secret in a GitHub *variable*, a `NUXT_PUBLIC_*` / `VITE_*` key holding a secret (those reach the browser), `SENTRY_ENVIRONMENT` as a GitHub var (it is pinned in `deployment.yaml`), `OPENSHIFT_*` at repo level, a `DeploymentConfig`.

## Scripts

All under `scripts/`, Python stdlib only, run from anywhere.

- `scaffold.py <repo> --app <app> --stack python|node --port N [--health /path] [--host-prod H] [--host-dev H] --config K1,K2,K3? [--secrets S1,S2?] [--sentry] [--migrate]` writes the files above. `--migrate` (python) adds a test job that applies the alembic migrations to a throwaway Postgres; use it whenever the app has migrations. `K?` = optional key, only sent when set (so an app default is not overridden by `""`). Never overwrites without `--force`.
- `audit.py <repo> [--app A] [--no-github]` checks manifests, RBAC, deploy.py, workflows and, read-only through `gh api`, the GitHub Environments (missing keys break the deploy, unused keys are drift, `OPENSHIFT_TOKEN` near its one-year expiry). Exit 1 on any ERROR.
- `gh_env.py <repo> --env dev|prod --file <KEY=value file> [--apply]` sets an Environment's vars/secrets, routing each key to var or secret by what `deploy.yml` reads; refuses keys no workflow reads. Dry run unless `--apply`.

## New project

1. Ask (or read from the code) before scaffolding: app name (kebab-case, no `-dev`), stack, container port, health path, prod/dev hosts, and the env keys the app reads, split into config vs secrets and required vs optional. Read the app's settings module (`config.py`, `nuxt.config.ts` runtimeConfig, `.env.example`), don't guess.
2. Run `scaffold.py`, then review by hand: resources, `strategy` (Recreate if a ReadWriteOnce PVC is mounted), extra containers (celery, redis), volumes. A PVC must exist in **each** namespace; until then use `emptyDir` with a comment, as `ovision-api` does.
3. `Dockerfile` must run as non-root on an arbitrary UID (OpenShift SCC), listen on the declared port, and for a Vite SPA write runtime config at start (`ovision/docker-entrypoint.sh` pattern); Nuxt reads `NUXT_PUBLIC_*` at runtime by itself.
4. Bootstrap the cluster and GitHub side: follow `references/bootstrap.md` step by step. Steps that run as a namespace admin or write to GitHub are done by the user, or by Claude only with explicit go-ahead for that step.
5. Run `audit.py` until it reports 0 errors, then push to `dev` and watch the Deploy run.

## Audit an existing repo

Run `audit.py <repo>` and report the findings grouped as **breaks a deploy** (ERROR), **drift / hygiene** (WARN), **compliant** (OK). Fixes to repo files go through a branch + PR from `dev` with a version bump, as for any change. Deleting GitHub vars/secrets flagged as unused, moving `OPENSHIFT_*` to Environments, or adding a branch policy changes shared settings: list the exact `gh` commands and run them only once the user approves.

Legacy shared-namespace repos (`genovine-*`, `data-explorer-*`, `metadata-api`, `cms-wagtail`, `genovalia-keycloak`, `betagene-*`) fail the namespace checks by design. Moving one to `ul-val-genovalia-dv` is a migration (PVC data, DNS, Keycloak redirect URIs, new CI token), not a fix: propose it, do not do it as part of an audit.

## Add, rename or remove a config key / secret

1. App code + `.env.example` first.
2. `deploy.yml`: in the right sync step, add `KEY: ${{ vars.KEY }}` (or `secrets.`) to `env:`, then the key either to the `for k in ...` required list and `args=(...)`, or as an `if [ -n "$KEY" ]; then args+=(...); fi` optional line. Keep keys sorted.
3. Set it in **both** Environments (`gh_env.py` or `gh variable set KEY --env dev`); a required key missing in one env fails that env's next deploy, on purpose.
4. Removing: delete from the workflow and from both Environments in the same change; the next deploy drops it from the ConfigMap.
5. Run `audit.py`.

## Exceptions (keep, don't "fix")

- `metadata-api`: Kustomize base + overlays because it is multi-tenant (sedna + csdcc). Its traps are in `references/lessons.md`.
- `genovine-backend`: `oc/prod/cronjob.yaml` + `db-migration.yml` (manual dev->prod data copy, suspended CronJob).
- `genovalia-keycloak`: prod only, no dev environment.

## References

- `references/bootstrap.md` - one-time cluster + GitHub setup for a new project, branch protection, token rotation.
- `references/lessons.md` - every rule above that comes from a past failure, with the reason. Read it before changing the templates or debugging a failed Deploy.
