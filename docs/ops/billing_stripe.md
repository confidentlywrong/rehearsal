# Stripe Billing — Operator Runbook

Scope: how to arm, smoke-test, and operate the Stripe integration that
drives `/api/v1/billing/checkout`, the billing-portal redirect, and the
`cmd/billing` webhook receiver. Skip this entire page if you are running
`SAAS_MODE=off` — none of these env vars are read in single-tenant mode.

## 1. Environment variables

All four must be set for self-serve upgrades to work. Any missing value
forces a graceful fallback to the stub creator, logged on boot.

| Variable | Read by | Required | Purpose |
| --- | --- | --- | --- |
| `BILLING_PROVIDER=stripe` | gateway, billing | yes | Master switch. Default `stub` rejects every event. |
| `STRIPE_API_KEY` | gateway | yes | Server-side secret (`sk_test_…` / `sk_live_…`). Arms outbound Checkout + Portal session creation. |
| `STRIPE_WEBHOOK_SECRET` | billing | yes | HMAC signing secret (`whsec_…`). `cmd/billing` refuses to start without it under `BILLING_PROVIDER=stripe`. |
| `STRIPE_PRICE_MAP` | gateway, billing | yes | Comma-separated `price_id:plan_tier` pairs. Used in BOTH directions: webhook → tier, and tier → checkout price. Both directions must agree, so configure once. |
| `STRIPE_RETURN_BASE_URL` | gateway | yes | Base URL the user bounces back to after Checkout/Portal. Local: `http://localhost:9118`. Cloud: `https://<your-frontend>`. |

`STRIPE_PRICE_MAP` syntax: `price_1ABCpro:pro,price_1XYZteam:team`.
Whitespace around the comma is allowed; trailing commas are not.

Boot-log signals (grep `GATEWAY:` and `BILLING:`):
- `GATEWAY: Stripe checkout creator armed (N tiers mapped)` — good.
- `GATEWAY: STRIPE_PRICE_MAP parse error, falling back to stub…` — typo in the map.
- `GATEWAY: STRIPE_RETURN_BASE_URL empty, falling back to stub…` — missing return URL.
- `GATEWAY: NewStripeCheckoutCreator failed, falling back to stub: <err>` — usually means the price map referenced a tier that does not exist in `configs/plans.yaml`.
- `BILLING: provider=stripe` + `BILLING: price map entries=N` — receiver is live.

## 2. Stripe Dashboard setup (one-time, per environment)

Do this once in **test mode** before going to production:

1. **Create the products + recurring prices** that match the tiers in
   `configs/plans.yaml` (currently `free`, `pro`, `team`). Note each
   `price_…` id.
2. **Add a webhook endpoint** pointing at `https://<your-domain>/webhook`
   (the `cmd/billing` service). Select these events — nothing more:
   - `checkout.session.completed`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_failed`
3. Copy the endpoint's **signing secret** into `STRIPE_WEBHOOK_SECRET`.
4. **Enable the Customer Portal** (Settings → Billing → Customer portal).
   Allow plan switching between the same prices you mapped in step 1,
   plus cancellation. The gateway's `StripePortalCreator` does not
   configure the portal — it only opens sessions against whatever you
   pre-configured here.
5. (Optional, cloud-only) Restrict the webhook endpoint's IP allowlist
   if your ingress supports it. Stripe publishes the source ranges at
   <https://stripe.com/files/ips/ips_webhooks.txt>.

## 3. Local smoke test (Stripe CLI, test mode)

You will need [`stripe` CLI](https://stripe.com/docs/stripe-cli) and a
test-mode account. Never use live keys against `make start`.

```bash
# 1. Log in to test mode
stripe login

# 2. Forward webhooks from Stripe to your local billing service.
#    Prints a whsec_… secret — copy it into .env.
stripe listen --forward-to http://localhost:9122/webhook

# 3. Configure .env (in a SECOND terminal)
cat >> .env <<'EOF'
SAAS_MODE=on
BILLING_PROVIDER=stripe
STRIPE_API_KEY=sk_test_xxx
STRIPE_WEBHOOK_SECRET=whsec_xxx  # from `stripe listen` output
STRIPE_PRICE_MAP=price_test_pro:pro,price_test_team:team
STRIPE_RETURN_BASE_URL=http://localhost:9118
EOF

# 4. Boot the stack
make restart

# 5. Drive a real Checkout from the UI
#    Open http://localhost:9118, go to Settings → Billing,
#    click "Upgrade to Pro". You should be redirected to
#    checkout.stripe.com with a test-mode banner.
#    Pay with test card 4242 4242 4242 4242, any future expiry, any CVC.

# 6. Watch the webhook arrive
#    The `stripe listen` terminal prints:
#      checkout.session.completed [evt_…]
#      customer.subscription.updated [evt_…]
#    And the billing service logs:
#      BILLING: applied event_id=evt_… org=… tier=pro
```

If step 6 shows the events but no `BILLING: applied …` line, grep the
billing logs for `unmapped price_id=` — your `STRIPE_PRICE_MAP` does not
include the price you actually charged for.

### Synthesising specific events without paying

```bash
# Force a subscription update (e.g. test the team-tier upgrade path)
stripe trigger customer.subscription.updated

# Force a failed payment to verify the payment_failed_at marker
stripe trigger invoice.payment_failed

# Force a cancellation to verify the downgrade-to-free path
stripe trigger customer.subscription.deleted
```

Verify side-effects with SQL:

```bash
docker compose exec postgres psql -U postgres -d racecondition -c \
  "SELECT id, plan_tier, stripe_customer_id, payment_failed_at
   FROM organizations ORDER BY created_at DESC LIMIT 5;"
```

## 4. Production cutover checklist

- [ ] Test-mode smoke run (section 3) green on staging.
- [ ] Live-mode products, prices, and Customer Portal configured.
- [ ] `STRIPE_API_KEY` swapped to `sk_live_…` in the secret manager.
- [ ] `STRIPE_WEBHOOK_SECRET` swapped to the live endpoint's signing secret.
- [ ] `STRIPE_PRICE_MAP` rewritten with the live `price_…` ids.
- [ ] `STRIPE_RETURN_BASE_URL` points at the production frontend domain
      (no trailing slash — the gateway strips one but multiple are confusing).
- [ ] Webhook endpoint in Stripe Dashboard subscribes to **exactly** the
      four events listed in section 2 (extras add latency and audit noise).
- [ ] A live-mode test purchase with a real card refunded after verifying
      the plan flipped in the database.

## 5. Failure modes & response

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Upgrade modal shows "self-serve upgrades not yet enabled" | Gateway booted with `STRIPE_API_KEY` empty or `STRIPE_PRICE_MAP` empty | Re-check env, restart gateway only (billing receiver does not need a restart) |
| Webhook returns 400 "invalid signature" | `STRIPE_WEBHOOK_SECRET` mismatch | Re-copy from Dashboard; remember test vs live secrets are different |
| Webhook returns 422 "price_id not in STRIPE_PRICE_MAP" | A new price was added in Stripe but not in env | Update `STRIPE_PRICE_MAP`, restart `cmd/billing`. The event will be redelivered automatically. |
| Webhook returns 422 "checkout session missing metadata.org_id" | Session was created outside the gateway (e.g. a Dashboard test) | Expected — those events have nowhere to land. Ignore. |
| Plan stuck on `free` after successful payment | Look for `BILLING: ignored event type=…` in logs | The Stripe endpoint is subscribed to extra event types. Either trim the subscription or accept the noise. |
| `payment_failed_at` set but no banner in UI | Frontend cache | User refreshes; the gateway re-reads it on every `/api/v1/usage`. |
| Duplicate plan changes in audit log | Dedup is in-memory only (Postgres dedup unavailable on boot) | Check `BILLING: postgres dedup unavailable` line at startup; reconnect the receiver to Postgres. Stripe will not redeliver already-acked events, so existing duplicates are historical and harmless. |

## 6. What is intentionally NOT here

- **No Stripe SDK.** The receiver implements HMAC-SHA256 verification +
  the JSON subset it cares about by hand in
  [internal/billing/stripe.go](../../internal/billing/stripe.go). The
  outbound creators in
  [internal/billing/stripe_checkout.go](../../internal/billing/stripe_checkout.go)
  and
  [internal/billing/stripe_portal.go](../../internal/billing/stripe_portal.go)
  use `net/http` + `url.Values`. This is deliberate — it keeps the
  dependency surface small and the verification logic auditable.
- **No automatic plan downgrades on `payment_failed`.** The webhook only
  sets `organizations.payment_failed_at` and emits an audit row. Whether
  to dunning-email, lock features, or downgrade the org is a product
  decision that lives outside the receiver.
- **No scheduled reconciliation job.** Per project rule, agents and
  periodic jobs are forbidden. If you need to re-sync state with Stripe,
  trigger it manually from the Admin UI (Phase 8 ships an
  "Org → Resync from Stripe" button you can wire to a script).
