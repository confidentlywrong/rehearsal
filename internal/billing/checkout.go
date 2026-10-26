// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

// checkout.go defines the CheckoutCreator abstraction the SPA's
// "Upgrade" button calls into. The real implementation
// (StripeCheckoutCreator in stripe_checkout.go) hits Stripe's
// /v1/checkout/sessions endpoint; the stub used by default refuses
// to open a session so a misconfigured local dev or test deploy can
// never silently take payment from a user.

package billing

import (
	"context"
	"errors"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

// ErrCheckoutUnavailable signals that the checkout provider is not
// configured (stub mode, missing API key, missing price map entry).
// Mapped to HTTP 503 by the Gin handler so the SPA shows a clean
// "billing not configured" state.
var ErrCheckoutUnavailable = errors.New("billing: checkout provider unavailable")

// CheckoutCreator opens a hosted-checkout session for one user on
// one plan tier and returns the URL the browser should be
// redirected to. Implementations MUST be safe for concurrent use
// (the HTTP handler may invoke Create from many goroutines).
type CheckoutCreator interface {
	// Name identifies the provider for logs and /healthz.
	Name() string
	// Create returns the redirect URL for an authenticated user
	// purchasing the given plan tier. successURL and cancelURL
	// are the post-checkout redirect targets; the SPA passes its
	// own deep links. Returns ErrCheckoutUnavailable when the
	// provider is not configured for the requested tier.
	Create(ctx context.Context, user account.User, tier account.PlanTier, successURL, cancelURL string) (string, error)
}

// StubCheckoutCreator is the default — always returns
// ErrCheckoutUnavailable so an unconfigured deploy never opens a
// broken redirect or accidentally takes payment.
type StubCheckoutCreator struct{}

// NewStubCheckoutCreator returns the always-unavailable creator.
func NewStubCheckoutCreator() *StubCheckoutCreator { return &StubCheckoutCreator{} }

// Name implements CheckoutCreator.
func (s *StubCheckoutCreator) Name() string { return "stub" }

// Create implements CheckoutCreator — always returns unavailable.
func (s *StubCheckoutCreator) Create(_ context.Context, _ account.User, _ account.PlanTier, _, _ string) (string, error) {
	return "", ErrCheckoutUnavailable
}
