# Operator Access — admin / dash / tester

This runbook explains how to reach the operator-only Cloud Run services
(`admin`, `dash`, `tester`) after they have been moved off the public
`allUsers` IAM binding to an explicit allow-list of operator
principals.

## Why these three are gated

`admin`, `dash`, and `tester` are operator surfaces — never used by
customers. They are bound at the GCP edge to `roles/run.invoker` for
the principals listed in `var.operator_principals`
([infra/modules/cloud-run-iam/main.tf](../../infra/modules/cloud-run-iam/main.tf)).

`tester` is the most important one to keep locked: it proxies to
Vertex Reasoning Engines using the runtime service account's OIDC
token, so any unauthenticated caller would be able to burn Vertex
quota under your billing account.

Customer-facing services (`frontend`, `gateway`, `billing`) are
unaffected and remain `allUsers` — they are protected at the
application layer (Firebase ID-token verification, Stripe signature
verification).

Cost of this posture: **$0/mo**. Cloud Run IAM is a free GCP
feature.

## Granting yourself access

Add your Google identity to `infra/terraform.tfvars`:

```hcl
operator_principals = [
  "user:you@example.com",
  # "group:ops-team@example.com",
  # "serviceAccount:ci@your-project.iam.gserviceaccount.com",
]
```

Then apply (or wait for the next Cloud Build to run terraform).

> Members must use the IAM principal prefix: `user:`, `group:`,
> `serviceAccount:`, `domain:`, `principal:`, or `principalSet:`.
> A bare email without `user:` will fail Terraform validation.

## Reaching admin / dash / tester from a browser

Three options, from least friction to most explicit:

### Option 1 — gcloud Cloud Run proxy (recommended)

`gcloud` will tunnel localhost through an authenticated proxy that
injects your ID token into every request:

```bash
gcloud run services proxy admin \
  --region=us-central1 \
  --project=<your-project> \
  --port=8080
# now browse: http://localhost:8080
```

Stop with `Ctrl-C`. Same pattern for `dash` and `tester`:

```bash
gcloud run services proxy dash    --region=us-central1 --port=8081
gcloud run services proxy tester  --region=us-central1 --port=8082
```

You can run all three in separate terminals.

### Option 2 — browser extension

Install a "Cloud Run Service Invoker" Chrome / Firefox extension that
injects:

```
Authorization: Bearer $(gcloud auth print-identity-token)
```

into every request to `*.run.app` URLs. The extension reads your
local gcloud credentials. This avoids the localhost tunnel and lets
you hit the live `*.run.app` URL directly.

### Option 3 — curl with token (for scripted checks)

```bash
TOKEN=$(gcloud auth print-identity-token)
ADMIN_URL=$(gcloud run services describe admin \
  --region=us-central1 --format='value(status.url)')
curl -H "Authorization: Bearer $TOKEN" "$ADMIN_URL/admin/users"
```

## Verifying the lockdown

After the next deploy, confirm `admin` / `dash` / `tester` reject
unauthenticated requests:

```bash
for svc in admin dash tester; do
  url=$(gcloud run services describe "$svc" \
    --region=us-central1 --format='value(status.url)')
  code=$(curl -sS -o /dev/null -w '%{http_code}' "$url/")
  echo "$svc: HTTP $code (expect 401 or 403)"
done
```

`frontend` should remain `200`:

```bash
url=$(gcloud run services describe frontend \
  --region=us-central1 --format='value(status.url)')
curl -sS -o /dev/null -w '%{http_code}\n' "$url/"
```

## Defense in depth — `TESTER_REQUIRE_AUTH`

Set `TESTER_REQUIRE_AUTH=true` on the tester service. When set, the
binary requires every request to carry an `Authorization: Bearer …`
header at the application layer. Since Cloud Run with IAM
enforcement only forwards requests that already carried a valid
Google ID token, this is a belt-and-suspenders check that surfaces
a 401 inside the container if anyone ever flips the IAM binding to
`allUsers` by mistake. See [cmd/tester/main.go](../../cmd/tester/main.go).

Local dev (no Cloud Run frontend, no IAM): leave `TESTER_REQUIRE_AUTH`
unset and the check is bypassed.

## Adding / removing operators

Edit `operator_principals` in tfvars and re-apply. The module uses
`for_each` so individual entries can be added or removed without
disturbing any other binding.

## Rollback (re-public, NOT recommended)

If you must temporarily restore browser-tab access for ad-hoc
debugging, prefer scoping to a dedicated principal (a colleague's
email or a short-lived service account) rather than reverting to
`allUsers`. Re-applying with an enlarged `operator_principals` list
is non-destructive and reversible. Re-binding `allUsers` on `tester`
is a Vertex-budget incident waiting to happen.

## Related

- [infra/modules/cloud-run-iam/variables.tf](../../infra/modules/cloud-run-iam/variables.tf) — full rationale for the customer/operator split.
- [audit_log_retention.md](audit_log_retention.md) — operator-side sweep procedure.
