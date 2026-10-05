---
name: openshift-deploy
description: Genovalia standard for deploying an app to the UL OpenShift cluster (ul-val-genovalia-dv / -pr) - Kustomize manifests (oc/base + oc/<stage>/, or oc/<tenant>/<stage>/ when one app is deployed for several tenants), CI service account RBAC, deploy.py, GitHub workflows (deploy, test, version-bump, restrict-main-source) and GitHub Environment vars/secrets synced into ConfigMap/Secret. Use when setting up deployment or CI/CD for a new Genovalia project, adding/removing a config variable or secret of a deployed app, auditing or realigning an existing repo's oc/ folder, workflows or GitHub environments, or debugging a failed Deploy run.
---

# OpenShift deploy standard (Genovalia)

One layout for every deployed repo, so a new project is a scaffold plus a bootstrap, and any repo can be audited against it. Reference implementations: `ovision-api` (backend) and `ovision` (frontend) are the closest; `genovine-*` and `data-explorer-*` follow the same workflow logic but still use one shared namespace (see Legacy).

Decided 2026-10-05: **dev and prod in separate namespaces**, **Kustomize for every app**: `oc/base/` + one leaf per target, `oc/dev/` + `oc/prod/` for one tenant, `oc/<tenant>/<stage>/` for several (see [Kustomize layout](#kustomize-layout)), **names with the stage as a suffix** (`<app>-service-dev`: kustomize appends it after each base name), **prod deployed automatically on merge to `main`** (the human gate is the required review of the `dev` -> `main` PR), **CI may create Routes with a custom host** (`routes/custom-host` create, no admin step per new Route).

The team-facing description lives in two Confluence pages (space Genovalia), aligned with this skill on 2026-10-05:
- "Comptes de service GitHub CI pour auto-déploiement" (page 247562242): CI accounts per app, what each Role grants, GitHub Environments, token generation and rotation, troubleshooting, and **the table of every app's CI account**.
- "Versionnement, tests et releases" (page 246120451): versioning, test jobs, image naming, release flow, branch protection.

Keep them in sync: when a project is bootstrapped (or a legacy repo migrated to `-dv`), add or update its row in the account table, including its "Accès particuliers" (own Secret, `routes/custom-host`). When this standard changes, update the matching section of those pages in the same change. Propose the Confluence edit to the user and publish it only once approved. If a page and this skill disagree, read the page's history before assuming which one is right.

## The standard

```
deploy.py                       templates/deploy.py, only APP (and TENANTS) differ
oc/base/                        deployment, service, route, kustomization, nameref (Route -> Service)
oc/dev/  oc/prod/               one leaf per stage: kustomization.yaml (+ config.env, secret.env,
                                written by CI, gitignored). Several tenants: oc/<tenant>/<stage>/
oc/rbac/dev/  oc/rbac/prod/     service-account, role, role-binding: bootstrap-only (namespace
                                admin, once), one CI account per namespace
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
| Service / Route | `<app>-service-dev` / `<app>-route-dev` | `<app>-service` / `<app>-route` |
| Secret | `<app>-secrets-dev` | `<app>-secrets` |
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

GitHub is the source of truth for config values: `deploy.yml` writes `config.env` and `secret.env` into the leaf from the Environment on every deploy, then `deploy.py` builds, pushes, applies `oc kustomize <leaf>` (the generators create the ConfigMap `<name>` and Secret `<name>-secrets`), labels everything with `app.kubernetes.io/version` and restarts the Deployment. A prod deploy from `main` tags `v<version>`. Slack: `deployment-dev`, `deployment-prod`, failures in `github-ci`.

Never: a value in a committed manifest that belongs in GitHub, a secret in a GitHub *variable*, a `NUXT_PUBLIC_*` / `VITE_*` key holding a secret (those reach the browser), `SENTRY_ENVIRONMENT` as a GitHub var (it is pinned in `deployment.yaml`), `OPENSHIFT_*` at repo level, a `DeploymentConfig`.

**Prerequisite: the repo lives in the GitHub org `ulaval-recherche`.** The image registry and the cluster API are only reachable from that org's runner group `ul-git-pr-resul-recherche-runners` (`runs-on: group:` in `deploy.yml`); a repo elsewhere (e.g. `genovalia/`) cannot deploy, `ubuntu-latest` neither. The image is always tagged and pushed as `:<version>` (read from `pyproject.toml` / `package.json` by `deploy.py`) next to `:latest`. `audit.py` errors on all three.

**Versioning of deployed apps:** the version in `pyproject.toml` / `package.json` follows [SemVer](https://semver.org) and each version has a `## <version>` entry in the repo's `CHANGELOG.md` ([Keep a Changelog](https://keepachangelog.com)). `version-bump.yml` fails a PR that does not raise the version or lacks its entry; `scaffold.py` writes the `CHANGELOG.md` stub; `audit.py` checks both.

## Scripts

All under `scripts/`, Python stdlib only, run from anywhere.

- `scaffold.py <repo> --app <app> --stack python|node --port N [--health /path] [--host TARGET=HOST ...] --config K1,K2,K3? [--secrets S1,S2?] [--sentry] [--migrate] [--tenants T1,T2]` writes the files above (`--tenants` for the several-tenants layout). `--migrate` (python) adds a test job that applies the alembic migrations to a throwaway Postgres; use it whenever the app has migrations. `K?` = optional key, only sent when set (so an app default is not overridden by `""`). Never overwrites without `--force`.
- `audit.py <repo> [--app A] [--no-github]` checks manifests, RBAC, deploy.py, workflows and, read-only through `gh api`, the GitHub Environments (missing keys break the deploy, unused keys are drift, `OPENSHIFT_TOKEN` near its one-year expiry). Exit 1 on any ERROR.
- `gh_env.py <repo> --env <target> --file <KEY=value file> [--apply]` sets an Environment's vars/secrets, routing each key to var or secret by what `deploy.yml` reads; refuses keys no workflow reads. Dry run unless `--apply`.

## New project

1. Ask (or read from the code) before scaffolding: app name (kebab-case, no `-dev`), stack, container port, health path, prod/dev hosts, and the env keys the app reads, split into config vs secrets and required vs optional. Read the app's settings module (`config.py`, `nuxt.config.ts` runtimeConfig, `.env.example`), don't guess.
2. Run `scaffold.py`, then review by hand: resources, `strategy` (Recreate if a ReadWriteOnce PVC is mounted), extra containers (celery, redis), volumes. A PVC must exist in **each** namespace; until then use `emptyDir` with a comment, as `ovision-api` does.
3. `Dockerfile` must run as non-root on an arbitrary UID (OpenShift SCC), listen on the declared port, and for a Vite SPA write runtime config at start (`ovision/docker-entrypoint.sh` pattern); Nuxt reads `NUXT_PUBLIC_*` at runtime by itself.
4. Bootstrap the cluster and GitHub side: follow `references/bootstrap.md` step by step. Steps that run as a namespace admin or write to GitHub are done by the user, or by Claude only with explicit go-ahead for that step.
5. Run `audit.py` until it reports 0 errors, then push to `dev` and watch the Deploy run.
6. Add the app to the account table of the Confluence page "Comptes de service GitHub CI pour auto-déploiement" (see above).

## Audit an existing repo

Run `audit.py <repo>` and report the findings grouped as **breaks a deploy** (ERROR), **drift / hygiene** (WARN), **compliant** (OK). Fixes to repo files go through a branch + PR from `dev` with a version bump, as for any change. Deleting GitHub vars/secrets flagged as unused, moving `OPENSHIFT_*` to Environments, or adding a branch policy changes shared settings: list the exact `gh` commands and run them only once the user approves.

Legacy shared-namespace repos (`genovine-*`, `data-explorer-*`, `metadata-api`, `cms-wagtail`, `genovalia-keycloak`, `betagene-*`) fail the namespace checks by design. Moving one to `ul-val-genovalia-dv` is a migration (PVC data, DNS, Keycloak redirect URIs, new CI token), not a fix: propose it, do not do it as part of an audit.

## Add, rename or remove a config key / secret

1. App code + `.env.example` first.
2. `deploy.yml`: add `KEY: ${{ vars.KEY }}` (or `secrets.`) to `env:` of "Render env files", the key to the required list, and a `printf` line in the `config.env` (var) or `secret.env` (secret) block, wrapped in `if [ -n ... ]` when optional. Keep keys sorted.
3. Set it in **every** Environment (`gh_env.py` or `gh variable set KEY --env <target>`); a required key missing in one fails that target's next deploy, on purpose.
4. Removing: delete from the workflow and from every Environment in the same change; the next deploy drops it from the ConfigMap.
5. Run `audit.py`.

## Kustomize layout

Same mechanism for every app, two shapes:

```
one tenant (default)           several tenants (same app per client / catalogue, e.g. metadata-api)
oc/base/                       oc/base/
oc/dev/  oc/prod/              oc/<tenant>/dev/  oc/<tenant>/prod/     (oc/sedna/dev, oc/csdcc/prod)
oc/rbac/dev/  oc/rbac/prod/    oc/rbac/dev/  oc/rbac/prod/             one CI account per namespace
deploy.py  TENANTS = []        deploy.py  TENANTS = ["sedna", "csdcc"]
.github/workflows/deploy.yml   templates/workflows/deploy-kustomize.yml (both)
```

| | one tenant | per target `<tenant>-<stage>` |
|---|---|---|
| Target = GitHub Environment | `dev`, `prod` | `<tenant>-dev`, `<tenant>-prod` (the first tenant too: no bare `dev`/`prod`) |
| Deployment, ConfigMap, ImageStream, `app` label | `<app>[-dev]` | `<tenant>-<app>[-dev]` |
| Service / Route / Secret | `<app>-service[-dev]` / `-route[-dev]` / `-secrets[-dev]` | `<tenant>-<app>-service[-dev]` etc. |
| Leaf | `oc/<stage>/` | `oc/<tenant>/<stage>/` |
| Host (default) | `<app>[-dev].apps.genovalia.ulaval.ca` | `<tenant>-<app>[-dev].apps.genovalia.ulaval.ca`, `--host TARGET=HOST` to override |

- Each leaf declares **everything** that varies: `namespace`, `namePrefix` (tenants only) / `nameSuffix` (dev), `labels`, `images`, the generators and the Route host patch. Never move any of it to an intermediate layer, and never add one (lessons: Kustomize). `base/` stays neutral: no namespace, names, images or values.
- A piece only some tenants need (sidecar, PVC): a patch in that leaf; once three or more leaves need the same one, a `components/` entry each leaf lists explicitly (it must not set namespace, names or generators).
- The ConfigMap and Secret come from `configMapGenerator`/`secretGenerator` reading `config.env`/`secret.env`, not from `oc create configmap`: kustomize only renames references to objects of its own build. `SENTRY_ENVIRONMENT` is a generator `literal`. `disableNameSuffixHash: true`, because the Role scopes by name.
- `deploy.yml`: a `targets` job lists the stage's targets (`deploy.py --list-targets`), the `deploy` job runs once per target (matrix, `fail-fast: false`, `environment: <target>`), and a final `tag` job tags once after all prod targets succeeded. A manual run deploys one target.
- Running `deploy.py` by hand needs the leaf's `config.env`/`secret.env` locally (they are gitignored); prefer a manual workflow run.
- Adding a tenant: `oc/<tenant>/{dev,prod}/` (copy a sibling, change prefix, image, host), add it to `TENANTS` and to the `workflow_dispatch` options, add its names to both Roles (namespace admin re-applies them), create its two Environments.
- Migrating a repo still on flat manifests (`oc/dev/deployment.yaml`, `oc create configmap` in the workflow) or on `oc/overlays/`: `git mv` the leaves to `oc/<tenant>/<stage>/`, set `resources: ../../base`, and for flat manifests scaffold into a scratch dir and port the differences. `audit.py` reports both.

## Exceptions (keep, don't "fix")

- `metadata-api`: the model for the several-tenants layout, but not aligned yet: shared namespace, bare `dev`/`prod` targets for sedna, `-db` Secret, image names with the tenant as suffix, no `restrict-main-source`/`version-bump` workflows. Tracked in DEV-391 (targets), DEV-392 (names), DEV-393 (dev namespace); `audit.py` reports all of it.
- `genovine-backend`: `oc/prod/cronjob.yaml` + `db-migration.yml` (manual dev->prod data copy, suspended CronJob).
- `genovalia-keycloak`: prod only, no dev environment.

## References

- `references/bootstrap.md` - one-time cluster + GitHub setup for a new project, branch protection, token rotation.
- `references/lessons.md` - every rule above that comes from a past failure, with the reason. Read it before changing the templates or debugging a failed Deploy.
