-- Copyright 2026 The Confidently Wrong Authors
--
-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at
--
--     http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.

-- migrations/0001_users.sql
--
-- Per-user data model. There are NO organizations and NO memberships
-- in this codebase. Every user owns exactly their own data:
--   * billing relationship (one Stripe customer per user)
--   * subscription / plan tier
--   * quota envelope
--   * usage ledger + rollup
--   * audit trail
--   * GDPR export & erase
--
-- Operator (superadmin) authority is a per-user flag, not a separate
-- "system org". The first superadmin must be promoted manually via a
-- direct SQL UPDATE on users.is_superadmin (see HARD RULE comment in
-- infra/terraform.tfvars). The env-var bootstrap was removed in the
-- 2026-06-23 hardening pass to eliminate a DR-time re-promotion risk.
--
-- Applied automatically on cold start by internal/account.ApplyMigration
-- with a project-wide pg_advisory_lock so multiple services racing on
-- the same Postgres instance serialize on the seed INSERTs.
--
-- All statements are idempotent (CREATE ... IF NOT EXISTS, seed INSERT
-- guarded by NOT EXISTS) so re-running on every boot is safe.

BEGIN;

-- --- users (identity + billing + role -- everything per user) ----------

CREATE TABLE IF NOT EXISTS users (
    id                 TEXT        PRIMARY KEY,           -- Firebase UID
    email              TEXT        NOT NULL,
    display_name       TEXT,
    timezone           TEXT,
    stripe_customer_id TEXT        UNIQUE,
    plan_tier          TEXT        NOT NULL DEFAULT 'free',
    payment_failed_at  TIMESTAMPTZ,
    is_superadmin      BOOLEAN     NOT NULL DEFAULT false,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at         TIMESTAMPTZ
);

-- P3.A (2026-06-22 audit): no plain-email index — every email lookup
-- in this codebase filters `deleted_at IS NULL`, so the partial
-- unique index `users_email_live_uniq` (created below) is the only
-- thing Postgres needs. The previous unconditional
-- `CREATE INDEX users_email_idx ON users(email)` duplicated that
-- index 1:1 for write cost without ever serving a read; DROP it on
-- cold start to clean up existing prod DBs.
DROP INDEX IF EXISTS users_email_idx;
CREATE INDEX IF NOT EXISTS users_deleted_at_idx  ON users(deleted_at);
CREATE INDEX IF NOT EXISTS users_stripe_cust_idx ON users(stripe_customer_id);
-- partial index: hot path for the admin UI's "list current superadmins"
-- query and any future audit jobs that count live operator rows.
CREATE INDEX IF NOT EXISTS users_superadmin_idx  ON users(is_superadmin) WHERE is_superadmin = true;

-- 2026-06-22: drop the legacy column-level UNIQUE(email) and replace
-- it with a partial unique index that only enforces uniqueness across
-- LIVE rows (deleted_at IS NULL). The old constraint blocked
-- legitimate re-signups after a soft-delete because the deleted row
-- still held the email; a new signup with the same address would
-- pass the GetUserByEmail pre-check (which filters deleted_at IS NULL)
-- and then trip 23505 on INSERT.
--
-- The CREATE TABLE above no longer specifies inline UNIQUE on email,
-- so a brand-new DB never gets the legacy constraint; an existing
-- prod DB has the constraint named `users_email_key` (Postgres's
-- default for inline UNIQUE) — DROP IF EXISTS handles both paths
-- idempotently. Reads against deleted rows (audit, GDPR export, admin
-- tooling) do a sequential scan when needed — those paths are cold
-- and not worth a dedicated index.
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_key;
CREATE UNIQUE INDEX IF NOT EXISTS users_email_live_uniq
    ON users(email) WHERE deleted_at IS NULL;

-- P3 #17 (2026-06-20): refresh-token / session-cookie revocation.
-- Set to NOW() by Store.RevokeTokens (called from logoutHandler and
-- account-delete paths). The Firebase resolver compares the token's
-- `iat` (issued-at) claim against this column; tokens issued strictly
-- before this timestamp are rejected with ErrTokenRevoked. No Firebase
-- Admin SDK dependency required — the existing JWKS-based verifier just
-- gets one extra comparison after the DB lookup.
ALTER TABLE users ADD COLUMN IF NOT EXISTS tokens_invalid_after TIMESTAMPTZ;

-- --- approval queue (2026-06-26) --------------------------------------
--
-- Manual sign-up gate. Every new user row lands with status='pending'
-- and is invisible to the rest of the app until a superadmin flips it
-- to 'approved' via the admin dashboard. The gateway's Account
-- middleware refuses to serve any route other than /api/v1/me*,
-- /auth/exchange and /healthz when status != 'approved' (P1, 2026-06-26).
--
-- 'rejected' is a terminal state — the user keeps their Firebase
-- account but the app permanently refuses to provision data for them.
-- This avoids the email-enumeration leak that a hard delete would
-- create (a rejected user can re-sign-up under the same email and
-- discover their old row is still pending, which is fine UX-wise).
--
-- Why a CHECK constraint and not an ENUM: ALTER TYPE on a Postgres
-- enum is awkward (requires VALIDATE CONSTRAINT for every add); a
-- text column with CHECK lets a future status value be added by a
-- single ALTER TABLE DROP CONSTRAINT + ADD CONSTRAINT pair.
ALTER TABLE users ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.constraint_column_usage
         WHERE table_name = 'users' AND constraint_name = 'users_status_check'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT users_status_check
            CHECK (status IN ('pending', 'approved', 'rejected'));
    END IF;
END $$;
ALTER TABLE users ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS approved_by TEXT;

-- Backfill: every existing superadmin is auto-approved so the gate
-- does not lock the operator out of the admin dashboard the first time
-- this migration applies. Every other existing row stays 'pending'
-- (column default). Idempotent — re-running has no effect on rows
-- that are already 'approved'.
UPDATE users
   SET status = 'approved',
       approved_at = COALESCE(approved_at, created_at),
       approved_by = COALESCE(approved_by, 'migration:0001_user_approval')
 WHERE is_superadmin = TRUE
   AND deleted_at IS NULL
   AND status = 'pending';

-- Hot index for the admin dashboard's "Pending only" filter (default
-- view). Partial — only the pending rows need fast lookup; approved
-- rows are scanned in time order via the existing created_at index.
CREATE INDEX IF NOT EXISTS users_status_pending_idx
    ON users(created_at DESC) WHERE status = 'pending' AND deleted_at IS NULL;

-- --- plan catalog (tier -> caps) --------------------------------------

CREATE TABLE IF NOT EXISTS plans (
    tier                TEXT    PRIMARY KEY,
    monthly_token_cap   BIGINT  NOT NULL,    -- -1 = unlimited
    max_concurrent_runs INT     NOT NULL,    -- -1 = unlimited
    description         TEXT
);

-- Seed plan rows only if the table is empty. Caps are calibrated to
-- ~150k tokens per simulation run (same as the prior multi-tenant
-- shell -- the per-user model uses the same numbers).
INSERT INTO plans (tier, monthly_token_cap, max_concurrent_runs, description)
SELECT 'free',          300000,     1, 'Free tier (watermarked exports)'
WHERE NOT EXISTS (SELECT 1 FROM plans WHERE tier = 'free');

INSERT INTO plans (tier, monthly_token_cap, max_concurrent_runs, description)
SELECT 'pro',          3000000,     3, 'Standard'
WHERE NOT EXISTS (SELECT 1 FROM plans WHERE tier = 'pro');

INSERT INTO plans (tier, monthly_token_cap, max_concurrent_runs, description)
SELECT 'team',        15000000,    10, 'Power user'
WHERE NOT EXISTS (SELECT 1 FROM plans WHERE tier = 'team');

INSERT INTO plans (tier, monthly_token_cap, max_concurrent_runs, description)
SELECT 'enterprise',        -1,    -1, 'Unlimited (custom SLA)'
WHERE NOT EXISTS (SELECT 1 FROM plans WHERE tier = 'enterprise');

-- 'comp' is the complimentary tier: manually granted by a superadmin
-- via PUT /admin/users/:id/plan. The Stripe webhook handler and the
-- reconciler both refuse to overwrite this tier so an inactive
-- subscription will not silently downgrade a comped user back to
-- free. Cap is unlimited tokens with a modest concurrency ceiling
-- (mirrors the enterprise plan but stays distinct in audit/usage
-- reports so paying enterprise customers can be told apart from
-- comped accounts). Operators reviewing the comp list should query:
--   SELECT id, email, created_at FROM users
--    WHERE plan_tier = 'comp' AND deleted_at IS NULL;
INSERT INTO plans (tier, monthly_token_cap, max_concurrent_runs, description)
SELECT 'comp',              -1,     5, 'Complimentary access (superadmin grant)'
WHERE NOT EXISTS (SELECT 1 FROM plans WHERE tier = 'comp');

-- --- usage metering (per user) ----------------------------------------

CREATE TABLE IF NOT EXISTS usage_ledger (
    id                BIGSERIAL   PRIMARY KEY,
    user_id           TEXT        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sim_id            TEXT,
    agent_type        TEXT,
    model             TEXT,
    prompt_tokens     BIGINT,
    completion_tokens BIGINT,
    total_tokens      BIGINT,
    cost_micros       BIGINT,
    ts                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS usage_ledger_user_ts_idx ON usage_ledger(user_id, ts);
CREATE INDEX IF NOT EXISTS usage_ledger_sim_idx     ON usage_ledger(sim_id);

CREATE TABLE IF NOT EXISTS usage_rollup_monthly (
    user_id       TEXT        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    year_month    TEXT        NOT NULL,                     -- 'YYYY-MM'
    total_tokens  BIGINT      NOT NULL DEFAULT 0,
    cost_micros   BIGINT      NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, year_month)
);

-- --- audit log (per user, ON DELETE SET NULL so erase keeps the trail) -

CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL   PRIMARY KEY,
    user_id     TEXT        REFERENCES users(id) ON DELETE SET NULL,
    actor_id    TEXT,                                       -- who acted (superadmin email for cross-user ops)
    action      TEXT        NOT NULL,
    target      TEXT,
    metadata    JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_log_user_created_idx ON audit_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS audit_log_action_idx       ON audit_log(action);

-- --- billing webhook dedup --------------------------------------------
--
-- Backs internal/billing.PostgresEventDedup. Every Stripe webhook
-- INSERTs its event_id here BEFORE applying the side-effect; a
-- duplicate delivery (Stripe retries on 5xx, on network-dropped 2xx,
-- and on operator-triggered replays) hits the ON CONFLICT branch and
-- the handler returns 200 without re-applying. Without this table the
-- INSERT fails with `relation does not exist`, dedup fails-open, and
-- every Stripe retry re-applies — duplicating audit_log rows even
-- though SetPlan itself is idempotent.
--
-- received_at is recorded for operator forensics (when did we first
-- see this event?) but isn't read by any code path; a future cleanup
-- job can use it to age out rows older than Stripe's retry window
-- (~3 days). Until that job exists, this table grows monotonically —
-- acceptable: at ~50 bytes/row and current webhook volume, it takes
-- many years to reach 1 GB.
CREATE TABLE IF NOT EXISTS processed_webhook_events (
    event_id    TEXT        PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index supports the (future) age-out DELETE without a sequential scan.
CREATE INDEX IF NOT EXISTS processed_webhook_events_received_at_idx
    ON processed_webhook_events(received_at);

COMMIT;
