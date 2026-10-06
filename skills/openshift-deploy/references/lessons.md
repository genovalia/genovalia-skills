# Lessons (why the templates look the way they do)

Each entry is a failure that already happened in one of the Genovalia repos. Don't undo one of these rules without reading its entry.

## RBAC

- **`create` cannot be scoped by `resourceNames`.** The object does not exist yet, so there is no name to match: a `create` verb inside a name-scoped rule is silently denied. Every resource gets a scoped rule (get/list/watch/update/patch) plus a separate unscoped `create` rule. (ovision, ovision-api, 2026-09-22)
- **`routes/custom-host`: `create` only.** A Route with an explicit `spec.host` needs this subresource; the `routes` rules don't cover it. Asking for `update` too is self-escalation (the admin ClusterRole only has `create`), and the whole Role is refused. (ovision, 2026-09-22)
- **RBAC files are bootstrap-only.** The CI account has no rights on serviceaccounts/roles/rolebindings; applying them with the leaf fails with a 403 on every deploy. They live in `oc/rbac/<stage>/`, outside every leaf (earlier, flat layout: `deploy.py` excluded them by name). (ovision, 2026-09-22)
- **ImageStream rules are needed to push.** The registry checks `imagestreams/layers` get/update on the target name; the ImageStream is created on first push, so `imagestreams` create too.
- **Bound tokens, not token Secrets.** `oc create token --duration=8760h` gives a token stored nowhere in the cluster that dies after a year; a `kubernetes.io/service-account-token` Secret never expires and anyone with Secret access in the namespace can read it. The price is a yearly rotation that nothing reminds anyone of: every token set on 2026-07-17 expires around 2027-07-17. `audit.py` warns 30 days ahead.
- **Names are scoped because namespaces are shared by several apps.** That is why ConfigMap/Secret names must be predictable (and why metadata-api disables kustomize's hash suffix).

## deploy.py

- **`docker login -u unused`.** A service account's username is `system:serviceaccount:ns:name`; Basic Auth splits user:pass on the first colon and corrupts both. The registry only checks the token.
- **Always `rollout restart`.** The image tag stays `:latest`, and ConfigMap/Secret changes are only read at pod start: without a restart, a config-only change never applies.
- **`DeploymentConfig` is gone.** Deprecated by OpenShift, and it has no `rollout restart` (needs `rollout latest`). Migrated repo by repo since 2026-07 (DEV-224).

## Workflows

- **No `$([ "$ENV" = dev ] && echo -dev)`.** On prod the test fails, the substitution returns 1 and `bash -e` aborts the step: every prod deploy failed. The template computes `RESOURCE_NAME` once in the job `env:`. (genovine-frontend #23, data-explorer #22/#28, 2026-09-15)
- **Optional keys are only sent when set.** `--from-literal=SMTP_PORT=""` overrides the app's default with an empty string (`int("")` crash). Required keys are checked non-empty instead, so a missing var fails the deploy loudly rather than shipping an empty config.
- **Tag only from `main`.** `workflow_dispatch` can deploy prod from any ref; tagging that commit labels a release that was not built from main. The prod Environment's branch policy now blocks it upstream too.
- **Concurrency per target env, not per ref.** Two manual prod deploys from different refs must not run at once.
- **`SENTRY_ENVIRONMENT` is pinned in `deployment.yaml`.** As a free-form GitHub var it reached Sentry under six different names and the prod alert rules matched nothing. (genovine-backend #49, metadata-api, 2026-09-24)

## GitHub Environments

- **Frontends got the backend's vars and secrets.** Environments were copied, then the workflows were split (2026-09-15) but the GitHub values stayed: ~15 unused vars and backend secrets (DB password, Keycloak client secret) still sit in frontend Environments. A frontend only holds its public runtime keys. `audit.py` reports these as drift.
- **Paginate.** The environment variables endpoint returns 10 items per page by default (30 at most): a check that reads one page misses keys. `audit.py` uses `gh api --paginate`.
- **`OPENSHIFT_*` per Environment.** With separate namespaces, a repo-level `OPENSHIFT_NAMESPACE`/`OPENSHIFT_TOKEN` points both envs at one namespace with one token.

## Manifests

- **`spec.selector` is immutable.** Select on `app` only, from the first deploy; adding a label to the selector later makes every apply fail with "field is immutable". Extra labels go on metadata and the pod template.
- **LimitRange floor.** Requests under the namespace's minimum are rejected at pod creation (redis at 25m, genovine-backend, 2026-09-22). Check `oc get limitrange` before right-sizing down.
- **Recreate with an RWO PVC.** A rolling update starts the new pod before the old one releases the volume and hangs.
- **No PVC in `ul-val-genovalia-dv` yet.** ovision-api uses `emptyDir` with a comment until one is provisioned; uploads are lost on restart in dev.

## Kustomize (every app; learned on metadata-api)

Encoded in `templates/kustomize/` and checked by `audit.py`:
- OpenShift `Route` is unknown to kustomize: declare `nameReference` for `spec.to.name`, or a rename leaves the Route pointing at a dead Service.
- `namePrefix`/`nameSuffix` and the generators must be in the same (leaf) kustomization, or the Deployment keeps pointing at another tenant's ConfigMap.
- `disableNameSuffixHash: true`, because the CI Role scopes access by name.
- Overlay without `namespace:`: the generated ConfigMap/Secret and the references to them are matched per namespace; set it in every leaf, not in the base.
- `labels` with `includeSelectors` for `app` only; `environment` goes in with `includeTemplates`, or the immutable selector changes and every apply fails.
- Several prod targets must not each push the release tag: they race on the same `v<version>`. The workflow tags once, in a job after the whole matrix.
- `deploy.py` renders the leaf once (`oc kustomize`) and feeds the same output to `oc apply` and `oc label`: `oc label -k` is not supported everywhere.
