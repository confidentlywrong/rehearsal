// Copyright 2026 Christian Haeussinger. Apache-2.0.

// price_map.go maps Stripe price IDs onto our four plan tiers.
//
// The mapping is operator-configured via the STRIPE_PRICE_MAP env
// var (comma-separated `priceID=tier` pairs, e.g.
// `price_pro_monthly=pro,price_team_monthly=team`). It is loaded
// once at process start by cmd/billing/main.go; the webhook handler
// looks up each incoming Stripe price against the map and refuses
// (422) any unmapped value rather than guess. Per the "never invent
// data" rule, a missing mapping is a configuration error and stops
// retries instead of silently promoting the wrong user.

package billing

import (
	"fmt"
	"strings"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

// PriceTierMap is the parsed form of STRIPE_PRICE_MAP. Lookups are
// O(1) and case-sensitive on the price id (Stripe price ids are
// case-sensitive in their API).
type PriceTierMap map[string]account.PlanTier

// LoadPriceMapFromEnv parses the comma-separated env var string
// into a PriceTierMap. Empty input returns an empty map and a nil
// error so a fresh deploy can boot with no Stripe wiring yet. A
// malformed entry (missing `=`, blank price id, unknown tier) is a
// fatal error — better to refuse to start than to silently promote
// the wrong user.
func LoadPriceMapFromEnv(raw string) (PriceTierMap, error) {
	out := PriceTierMap{}
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return out, nil
	}
	for _, pair := range strings.Split(raw, ",") {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		idx := strings.IndexByte(pair, '=')
		if idx <= 0 || idx == len(pair)-1 {
			return nil, fmt.Errorf("STRIPE_PRICE_MAP: malformed entry %q (want priceID=tier)", pair)
		}
		priceID := strings.TrimSpace(pair[:idx])
		tier := account.PlanTier(strings.TrimSpace(pair[idx+1:]))
		if priceID == "" {
			return nil, fmt.Errorf("STRIPE_PRICE_MAP: blank price id in entry %q", pair)
		}
		if !tier.IsKnown() {
			return nil, fmt.Errorf("STRIPE_PRICE_MAP: unknown tier %q (want one of free|pro|team|enterprise)", tier)
		}
		if _, dup := out[priceID]; dup {
			return nil, fmt.Errorf("STRIPE_PRICE_MAP: duplicate price id %q", priceID)
		}
		out[priceID] = tier
	}
	return out, nil
}

// Lookup returns the tier configured for priceID, or false when the
// id is not in the map. Callers MUST handle the false case as a
// 422 (operator configuration error), never silently substitute
// a default tier.
func (m PriceTierMap) Lookup(priceID string) (account.PlanTier, bool) {
	if m == nil {
		return "", false
	}
	t, ok := m[priceID]
	return t, ok
}

// PriceForTier returns the first price id that maps to tier, if any.
// Used by the SPA's "Upgrade to Pro" button to pick the right
// Checkout price without hard-coding the id in the frontend. Returns
// "" when no price is mapped to tier — the SPA renders a "billing
// not configured" message in that case.
func (m PriceTierMap) PriceForTier(tier account.PlanTier) string {
	for p, t := range m {
		if t == tier {
			return p
		}
	}
	return ""
}
