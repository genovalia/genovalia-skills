# Bootstrap a new project (once per repo)

Run after `scaffold.py` and the manual review. Set these first:

```bash
APP=<app>                       # as passed to scaffold.py
REPO=<owner>/<repo>             # GitHub repo
```

Each step that writes to the cluster or to GitHub is done by the user, or by Claude only with explicit approval of that step.

## 1. CI account in each namespace (namespace admin)

The CI service account cannot create its own RBAC; a human with admin rights on the namespace applies the bootstrap files once. `deploy.py` skips them afterwards.

```bash
oc login api.ul-pca-pr-ul01.ulaval.ca:6443        # personal account
for ENV in dev prod; do
  NS=$([ "$ENV" = dev ] && echo ul-val-genovalia-dv || echo ul-val-genovalia-pr)
  oc apply -n "$NS" -f oc/$ENV/service-account.yaml -f oc/$ENV/role.yaml \
                    -f oc/$ENV/role-binding.yaml
done
```

If the Role is refused with "attempting to grant RBAC permissions not currently held", the applying account lacks one of the verbs (see lessons: `routes/custom-host`).

Check the LimitRange floor of each namespace before trusting the scaffolded resources: `oc get limitrange -n <ns> -o yaml`.

## 2. GitHub Environments

```bash
gh api -X PUT repos/$REPO/environments/dev
gh api -X PUT repos/$REPO/environments/prod --input - <<'EOF'
{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
EOF
gh api -X POST repos/$REPO/environments/prod/deployment-branch-policies -f name=main -f type=branch
```

## 3. OpenShift token into each Environment

Bound tokens (`TokenRequest`), as in the Confluence page "Comptes de service GitHub CI pour auto-déploiement": stored nowhere in the cluster, valid 365 days, **never renewed automatically**. Pipe each one straight into GitHub so it is never displayed.

```bash
for ENV in dev prod; do
  NS=$([ "$ENV" = dev ] && echo ul-val-genovalia-dv || echo ul-val-genovalia-pr)
  oc create token github-ci-$APP -n "$NS" --duration=8760h \
    | gh secret set OPENSHIFT_TOKEN --env "$ENV" --repo "$REPO"
done
```

### Rotation

Yearly per app and per env, or at once on a suspected leak (token pasted somewhere, printed in a workflow log, compromised Action dependency). Re-run the loop above for the env concerned: the GitHub secret is replaced, other apps are unaffected. `audit.py` warns 30 days before the expected expiry (it dates the token by the GitHub secret's last update) and reports an ERROR after it.

A replaced token stays valid until it expires. To cut a leaked one off immediately, recreate the service account (`oc delete sa github-ci-$APP -n <ns>`, then step 1 and this step): every token bound to the old account dies with it.

Symptom of an expired token: `oc login --token=...` fails with `401` in the Deploy run.

## 4. Vars and secrets

Write one `KEY=value` file per environment **outside the repo** (scratch dir), with every key `deploy.yml` reads. `OPENSHIFT_CLUSTER` and `OPENSHIFT_NAMESPACE` are filled in automatically.

```bash
python3 <skill>/scripts/gh_env.py . --env dev  --file <scratch>/dev.env           # dry run
python3 <skill>/scripts/gh_env.py . --env dev  --file <scratch>/dev.env --apply
python3 <skill>/scripts/gh_env.py . --env prod --file <scratch>/prod.env --apply
```

Delete the files afterwards.

## 5. Slack

`SLACK_BOT_TOKEN` as a repo secret (`gh secret set SLACK_BOT_TOKEN --repo $REPO`), the same bot token as the other Genovalia repos; ask whoever administers the Slack app. The bot must be in `deployment-dev`, `deployment-prod` and `github-ci`.

## 6. Branches

`dev` exists and is the default branch for PRs. Protect both (no direct push, PR required) with these required status checks: `test`, `build` (plus `migrate` if scaffolded with `--migrate`) from test.yml, `version-bump`, and on `main` also `check-source-branch` (restrict-main-source.yml).

`main` also requires **1 approving review**. Merging to `main` deploys prod automatically, so the reviewer approves a `dev` -> `main` PR only after checking that this exact version runs correctly on dev.

```bash
gh api -X PUT repos/$REPO/branches/main/protection --input - <<'JSON'
{"required_status_checks": {"strict": false, "contexts": ["test", "build", "version-bump", "check-source-branch"]},
 "enforce_admins": false,
 "required_pull_request_reviews": {"required_approving_review_count": 1},
 "restrictions": null}
JSON
```

Same call for `dev` without `check-source-branch` and with `"required_pull_request_reviews": null`. Add `"migrate"` to the contexts when the repo has that job.

## 7. First deploy

```bash
python3 <skill>/scripts/audit.py .               # must report 0 errors
git push origin dev                              # Deploy workflow -> dev
gh run watch --repo $REPO
oc get pods -n ul-val-genovalia-dv -l app=$APP-dev
```

Then Keycloak (redirect URIs, web origins for both hosts) if the app uses it, and a PR `dev` -> `main` for prod.

## 8. Confluence

Add a row for the app to the account table of "Comptes de service GitHub CI pour auto-déploiement" (service account, namespaces `-dv` and `-pr`, RBAC files `oc/dev/` + `oc/prod/`, `OPENSHIFT_TOKEN` per Environment, special access: own Secret if it has one, `routes/custom-host`).
