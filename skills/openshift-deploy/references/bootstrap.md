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
                    -f oc/$ENV/role-binding.yaml -f oc/$ENV/ci-token.yaml
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

The token Secret is filled asynchronously by OpenShift: check it is non-empty, then pipe it straight into GitHub so it is never printed.

```bash
for ENV in dev prod; do
  NS=$([ "$ENV" = dev ] && echo ul-val-genovalia-dv || echo ul-val-genovalia-pr)
  TOKEN=$(oc get secret github-ci-$APP-token -n "$NS" -o jsonpath='{.data.token}' | base64 -d)
  [ -n "$TOKEN" ] || { echo "token not issued yet in $NS"; break; }
  printf %s "$TOKEN" | gh secret set OPENSHIFT_TOKEN --env "$ENV" --repo "$REPO"
done
unset TOKEN
```

Rotation (leak, or someone left): `oc delete secret github-ci-$APP-token -n <ns>`, re-apply `ci-token.yaml`, re-run this step. The old token stops working when its Secret is deleted.

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

`dev` exists and is the default branch for PRs. On `main` and `dev`, require PRs and the status checks `test` / `build` (test.yml), `version-bump`, and on `main` `check-source-branch` (restrict-main-source.yml).

## 7. First deploy

```bash
python3 <skill>/scripts/audit.py .               # must report 0 errors
git push origin dev                              # Deploy workflow -> dev
gh run watch --repo $REPO
oc get pods -n ul-val-genovalia-dv -l app=$APP-dev
```

Then Keycloak (redirect URIs, web origins for both hosts) if the app uses it, and a PR `dev` -> `main` for prod.
