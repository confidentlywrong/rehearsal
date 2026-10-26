// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

// reconcile.go is the operator-triggered "force-sync" between
// Stripe and the local users table for a single user. Used when a
// webhook was missed (Stripe outage, our service down for a
// window) and an operator needs to bring one user's plan_tier back
// in line with Stripe's view of their subscription.
//
// Idempotent: reading Stripe's current subscription and writing
// SetPlan with the resulting tier is a no-op if they already match.
// Mounted as POST /admin/billing/reconcile by cmd/admin.

package billing

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

// SubscriptionFetcher reads the current subscription state for one
// Stripe customer. The real implementation hits
// /v1/subscriptions?customer=...&status=all&limit=1 and decodes
// the first item; tests pass an in-memory fake.
type SubscriptionFetcher interface {
	// FetchActiveSubscription returns the price id of the
	// customer's currently-active subscription, or "" when no
	// active subscription exists (the user has cancelled or
	// never subscribed). NotFound is NOT an error — it signals
	// "downgrade to free".
	FetchActiveSubscription(ctx context.Context, customerID string) (priceID string, err error)
}

// PlanWriter is the subset of *account.Store the reconciler needs
// to update a user's plan_tier.
type PlanWriter interface {
	SetPlan(ctx context.Context, userID string, tier account.PlanTier) error
}

// Reconciler force-syncs one user's plan against Stripe.
type Reconciler struct {
	Users    UserLookup
	Fetcher  SubscriptionFetcher
	PriceMap PriceTierMap
	Writer   PlanWriter
}

// ReconcileResult is what Reconcile returns to the caller (admin
// handler) so it can be surfaced in the response and audit log.
type ReconcileResult struct {
	UserID    string            `json:"user_id"`
	FromTier  account.PlanTier  `json:"from_tier"`
	ToTier    account.PlanTier  `json:"to_tier"`
	PriceID   string            `json:"price_id,omitempty"` // empty when downgrading to free
	Changed   bool              `json:"changed"`
}

// Reconcile fetches the user's current Stripe subscription, maps
// its price to a tier, and writes the result to the users table.
// Returns the result with Changed=false when no write was needed.
//
// Errors:
//   - account.ErrNotFound when user_id is unknown.
//   - "user has no stripe customer" (string match acceptable for
//     callers) when StripeCustomerID is empty (nothing to reconcile).
//   - "unmapped price" when Stripe returns a price not in our map
//     (operator configuration error).
func (r *Reconciler) Reconcile(ctx context.Context, userID string) (ReconcileResult, error) {
	if r == nil || r.Users == nil || r.Fetcher == nil || r.Writer == nil {
		return ReconcileResult{}, errors.New("billing: Reconciler not fully configured")
	}
	user, err := r.Users.GetUser(ctx, userID)
	if err != nil {
		return ReconcileResult{}, err
	}
	// Comp grants are out-of-band: the user has no Stripe subscription
	// driving their tier. Reconciling would either hit the
	// "no stripe customer" branch below OR (if Stripe was used in the
	// past) downgrade the user to free because no active subscription
	// exists. Both are wrong — a superadmin set this tier deliberately.
	// Surface as a no-op so operator dashboards do not show spurious
	// reconcile failures for comped accounts.
	if user.PlanTier == account.PlanComp {
		return ReconcileResult{
			UserID:   user.ID,
			FromTier: account.PlanComp,
			ToTier:   account.PlanComp,
			Changed:  false,
		}, nil
	}
	if user.StripeCustomerID == "" {
		return ReconcileResult{}, errors.New("billing: user has no stripe customer id")
	}
	priceID, err := r.Fetcher.FetchActiveSubscription(ctx, user.StripeCustomerID)
	if err != nil {
		return ReconcileResult{}, fmt.Errorf("billing: fetch subscription: %w", err)
	}

	target := account.PlanFree
	if priceID != "" {
		t, ok := r.PriceMap.Lookup(priceID)
		if !ok {
			return ReconcileResult{}, fmt.Errorf("billing: unmapped price %q from stripe", priceID)
		}
		target = t
	}

	result := ReconcileResult{
		UserID:   user.ID,
		FromTier: user.PlanTier,
		ToTier:   target,
		PriceID:  priceID,
		Changed:  user.PlanTier != target,
	}
	if !result.Changed {
		return result, nil
	}
	if err := r.Writer.SetPlan(ctx, user.ID, target); err != nil {
		return ReconcileResult{}, fmt.Errorf("billing: set plan: %w", err)
	}
	return result, nil
}

// StripeSubscriptionFetcher is the real implementation that talks
// to Stripe's REST API. apiKey is required; apiBase defaults to
// https://api.stripe.com.
type StripeSubscriptionFetcher struct {
	apiKey     string
	apiBase    string
	httpClient *http.Client
}

// NewStripeSubscriptionFetcher constructs a fetcher.
func NewStripeSubscriptionFetcher(apiKey string) (*StripeSubscriptionFetcher, error) {
	if apiKey == "" {
		return nil, errors.New("billing: NewStripeSubscriptionFetcher requires apiKey")
	}
	return &StripeSubscriptionFetcher{
		apiKey:     apiKey,
		apiBase:    stripeAPIBase,
		httpClient: &http.Client{Timeout: 15 * time.Second},
	}, nil
}

// WithSubscriptionAPIBase overrides the Stripe API base URL (tests).
func (f *StripeSubscriptionFetcher) WithAPIBase(base string) *StripeSubscriptionFetcher {
	f.apiBase = strings.TrimRight(base, "/")
	return f
}

// WithHTTPClient overrides the http client (tests).
func (f *StripeSubscriptionFetcher) WithHTTPClient(c *http.Client) *StripeSubscriptionFetcher {
	f.httpClient = c
	return f
}

// FetchActiveSubscription implements SubscriptionFetcher.
func (f *StripeSubscriptionFetcher) FetchActiveSubscription(ctx context.Context, customerID string) (string, error) {
	if customerID == "" {
		return "", errors.New("billing: customerID required")
	}
	u := fmt.Sprintf("%s/v1/subscriptions?customer=%s&status=active&limit=1", f.apiBase, customerID)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return "", fmt.Errorf("build subscription request: %w", err)
	}
	req.SetBasicAuth(f.apiKey, "")
	req.Header.Set("Accept", "application/json")

	resp, err := f.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("subscription request failed: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("read subscription response: %w", err)
	}
	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("stripe subscriptions %d: %s", resp.StatusCode, truncateForLog(body))
	}
	var decoded struct {
		Data []struct {
			Items struct {
				Data []struct {
					Price struct {
						ID string `json:"id"`
					} `json:"price"`
				} `json:"data"`
			} `json:"items"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &decoded); err != nil {
		return "", fmt.Errorf("decode subscription response: %w", err)
	}
	if len(decoded.Data) == 0 || len(decoded.Data[0].Items.Data) == 0 {
		return "", nil // no active subscription → free tier
	}
	return decoded.Data[0].Items.Data[0].Price.ID, nil
}
