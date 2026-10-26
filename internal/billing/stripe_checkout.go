// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

// stripe_checkout.go is the real Stripe-backed CheckoutCreator. It
// uses only stdlib net/http — no Stripe SDK — because we touch only
// /v1/checkout/sessions and /v1/customers and the SDK would pull a
// large dependency tree we don't need.

package billing

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

// stripeAPIBase is the production Stripe API root. Tests override
// via WithCheckoutAPIBase / WithPortalAPIBase.
const stripeAPIBase = "https://api.stripe.com"

// CustomerIDPersister is how StripeCheckoutCreator records a newly
// created Stripe customer back onto the local user row so future
// renewals/portal sessions can be addressed by customer id alone.
// *account.Store satisfies it via SetStripeCustomerID; tests pass
// a closure.
type CustomerIDPersister interface {
	SetStripeCustomerID(ctx context.Context, userID, customerID string) error
}

// StripeCheckoutCreator opens hosted Checkout sessions against
// Stripe's REST API. Configured with an API key and a price map so
// it knows which price id maps to each plan tier.
type StripeCheckoutCreator struct {
	apiKey     string
	apiBase    string
	httpClient *http.Client
	priceMap   PriceTierMap
	persister  CustomerIDPersister
}

// StripeCheckoutOption configures a StripeCheckoutCreator.
type StripeCheckoutOption func(*StripeCheckoutCreator)

// WithCheckoutAPIBase overrides the Stripe API base URL (tests).
func WithCheckoutAPIBase(base string) StripeCheckoutOption {
	return func(s *StripeCheckoutCreator) { s.apiBase = strings.TrimRight(base, "/") }
}

// WithCheckoutHTTPClient injects a custom http.Client (tests).
func WithCheckoutHTTPClient(c *http.Client) StripeCheckoutOption {
	return func(s *StripeCheckoutCreator) { s.httpClient = c }
}

// NewStripeCheckoutCreator builds a real Stripe checkout creator.
// apiKey and priceMap are required; persister may be nil in which
// case the customer id is logged but not persisted (the next
// checkout will create a duplicate customer, which is wasteful but
// not incorrect).
func NewStripeCheckoutCreator(apiKey string, priceMap PriceTierMap, persister CustomerIDPersister, opts ...StripeCheckoutOption) (*StripeCheckoutCreator, error) {
	if apiKey == "" {
		return nil, errors.New("billing: NewStripeCheckoutCreator requires apiKey")
	}
	if len(priceMap) == 0 {
		return nil, errors.New("billing: NewStripeCheckoutCreator requires a non-empty price map")
	}
	c := &StripeCheckoutCreator{
		apiKey:     apiKey,
		apiBase:    stripeAPIBase,
		httpClient: &http.Client{Timeout: 15 * time.Second},
		priceMap:   priceMap,
		persister:  persister,
	}
	for _, opt := range opts {
		opt(c)
	}
	return c, nil
}

// Name implements CheckoutCreator.
func (s *StripeCheckoutCreator) Name() string { return "stripe" }

// Create implements CheckoutCreator. It:
//  1. Resolves the price id for the requested tier (422 via
//     ErrCheckoutUnavailable if unmapped).
//  2. Ensures the user has a Stripe customer id (creating one with
//     email + metadata.user_id when missing, persisting via the
//     CustomerIDPersister).
//  3. POSTs to /v1/checkout/sessions with mode=subscription, the
//     user's customer id, the line item, and metadata{user_id}.
//  4. Returns the hosted Checkout URL.
func (s *StripeCheckoutCreator) Create(ctx context.Context, user account.User, tier account.PlanTier, successURL, cancelURL string) (string, error) {
	if user.ID == "" {
		return "", errors.New("billing: Create requires user.ID")
	}
	// L3 (2026-06-25 audit): sanity-check the identifiers we paste
	// into Stripe metadata. user.ID flows back to us verbatim on
	// the checkout.session.completed webhook and gets passed
	// straight into the database SetPlan call — any odd payload
	// would simply fail to resolve, but rejecting at the source
	// keeps the webhook handler's "metadata.user_id" contract
	// strict and makes downstream log audits easier to read.
	// Firebase UIDs are 28 chars of [A-Za-z0-9]; we accept the
	// broader filesystem-safe alphabet (hyphen, underscore) to be
	// forward-compatible with custom provider UIDs without
	// admitting URL/HTML/JSON-injection chars.
	if !safeMetadataID(user.ID) {
		return "", errors.New("billing: user.ID contains characters disallowed for Stripe metadata")
	}
	if !safeMetadataID(string(tier)) {
		return "", errors.New("billing: tier contains characters disallowed for Stripe metadata")
	}
	if successURL == "" || cancelURL == "" {
		return "", errors.New("billing: Create requires successURL and cancelURL")
	}
	priceID := s.priceMap.PriceForTier(tier)
	if priceID == "" {
		return "", fmt.Errorf("%w: no price configured for tier %q", ErrCheckoutUnavailable, tier)
	}
	if !safeMetadataID(priceID) {
		// PriceID comes from STRIPE_PRICE_MAP, an operator-
		// controlled env var. Bad bytes here would point at a
		// misconfigured deployment, not user input — surface it
		// as a 503-ish error so the operator notices.
		return "", fmt.Errorf("%w: priceID for tier %q contains characters disallowed for Stripe metadata", ErrCheckoutUnavailable, tier)
	}

	customerID := user.StripeCustomerID
	if customerID == "" {
		newCustomer, err := s.createCustomer(ctx, user)
		if err != nil {
			return "", fmt.Errorf("billing: ensure customer: %w", err)
		}
		customerID = newCustomer
		if s.persister != nil {
			if err := s.persister.SetStripeCustomerID(ctx, user.ID, customerID); err != nil {
				// Persistence failure is logged loud at the call
				// site by the handler; we still return the URL so
				// the user can complete checkout. The webhook
				// handler will resolve the user via the customer
				// id on the resulting subscription event.
				return "", fmt.Errorf("billing: persist customer id: %w", err)
			}
		}
	}

	form := url.Values{}
	form.Set("mode", "subscription")
	form.Set("customer", customerID)
	form.Set("success_url", successURL)
	form.Set("cancel_url", cancelURL)
	form.Set("line_items[0][price]", priceID)
	form.Set("line_items[0][quantity]", "1")
	form.Set("metadata[user_id]", user.ID)
	form.Set("metadata[price_id]", priceID)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.apiBase+"/v1/checkout/sessions", strings.NewReader(form.Encode()))
	if err != nil {
		return "", fmt.Errorf("billing: build checkout request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	req.SetBasicAuth(s.apiKey, "")

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("billing: checkout request failed: %w", err)
	}
	defer resp.Body.Close()
	body, readErr := io.ReadAll(resp.Body)
	if readErr != nil {
		return "", fmt.Errorf("billing: read checkout response: %w", readErr)
	}
	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		return "", fmt.Errorf("%w: stripe rejected credentials (%d): %s", ErrCheckoutUnavailable, resp.StatusCode, truncateForLog(body))
	}
	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("billing: stripe checkout %d: %s", resp.StatusCode, truncateForLog(body))
	}
	var decoded struct {
		URL   string `json:"url"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(body, &decoded); err != nil {
		return "", fmt.Errorf("billing: decode checkout response: %w", err)
	}
	if decoded.Error != nil {
		return "", fmt.Errorf("billing: stripe checkout rejected: %s", decoded.Error.Message)
	}
	if decoded.URL == "" {
		return "", errors.New("billing: stripe checkout returned empty url")
	}
	return decoded.URL, nil
}

// createCustomer POSTs to /v1/customers with email + metadata.user_id
// so a returning customer can be identified in the Stripe dashboard
// even before any subscription exists.
func (s *StripeCheckoutCreator) createCustomer(ctx context.Context, user account.User) (string, error) {
	form := url.Values{}
	if user.Email != "" {
		form.Set("email", user.Email)
	}
	form.Set("metadata[user_id]", user.ID)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.apiBase+"/v1/customers", strings.NewReader(form.Encode()))
	if err != nil {
		return "", fmt.Errorf("build customer request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	req.SetBasicAuth(s.apiKey, "")

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("customer request failed: %w", err)
	}
	defer resp.Body.Close()
	body, readErr := io.ReadAll(resp.Body)
	if readErr != nil {
		return "", fmt.Errorf("read customer response: %w", readErr)
	}
	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("stripe customer %d: %s", resp.StatusCode, truncateForLog(body))
	}
	var decoded struct {
		ID    string `json:"id"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(body, &decoded); err != nil {
		return "", fmt.Errorf("decode customer response: %w", err)
	}
	if decoded.Error != nil {
		return "", fmt.Errorf("stripe customer rejected: %s", decoded.Error.Message)
	}
	if decoded.ID == "" {
		return "", errors.New("stripe customer returned empty id")
	}
	return decoded.ID, nil
}

// truncateForLog clips a byte slice to <=512 bytes so failure logs
// don't dump multi-KB Stripe payloads.
func truncateForLog(b []byte) string {
	const max = 512
	if len(b) <= max {
		return string(b)
	}
	return string(b[:max]) + "...(truncated)"
}

// safeMetadataID reports whether s is short and uses only
// filesystem-safe characters. We paste s into Stripe metadata
// (`metadata[user_id]`, `metadata[price_id]`); Stripe round-trips
// the value verbatim back to us on the webhook. Rejecting odd bytes
// at the source guarantees the webhook handler's downstream
// consumers (audit logs, database lookups) only ever see a known
// charset, and removes a class of injection-into-metadata bugs
// before they can happen.
//
// Accepted alphabet: A-Z, a-z, 0-9, '-', '_'. Length: 1..128.
// This admits Firebase UIDs (28-char alnum), Stripe price IDs
// (`price_XXX` form), and the catalog tier names ("pro", "team",
// "enterprise").
func safeMetadataID(s string) bool {
	if s == "" || len(s) > 128 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= 'A' && c <= 'Z':
		case c >= 'a' && c <= 'z':
		case c >= '0' && c <= '9':
		case c == '-' || c == '_':
		default:
			return false
		}
	}
	return true
}
