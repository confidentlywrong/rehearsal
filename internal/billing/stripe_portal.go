// Copyright 2026 The Confidently Wrong Authors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Stripe billing-portal session creation. This is the outbound
// side that powers the SPA's "Manage subscription" button on
// /settings/billing. The portal handles plan changes, payment
// method updates, invoices, and cancellation — Stripe's own UI
// renders all of those, so we just need to open a session and
// redirect the browser to it.

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
)

// ErrPortalUnavailable signals that the portal provider is not
// configured (stub mode, missing API key, missing customer id on
// the user). The handler maps this to a 503 with a stable error
// code so the SPA can render a friendly "billing portal not
// enabled" state instead of a generic 500.
var ErrPortalUnavailable = errors.New("billing: portal provider unavailable")

// PortalCreator creates a one-shot URL the user's browser can be
// redirected to in order to manage their existing subscription.
type PortalCreator interface {
	// Name identifies the provider for logging.
	Name() string
	// Create returns a redirect URL. customerID is the
	// stripe_customer_id stored on the users row; the handler
	// is responsible for looking it up and short-circuiting with
	// 409 when it's missing.
	Create(ctx context.Context, customerID, returnURL string) (string, error)
}

// StubPortalCreator is the default; always returns
// ErrPortalUnavailable so a misconfigured deploy never opens a
// broken redirect.
type StubPortalCreator struct{}

// NewStubPortalCreator returns the always-unavailable creator.
func NewStubPortalCreator() *StubPortalCreator { return &StubPortalCreator{} }

// Name implements PortalCreator.
func (s *StubPortalCreator) Name() string { return "stub" }

// Create implements PortalCreator — always returns unavailable.
func (s *StubPortalCreator) Create(_ context.Context, _, _ string) (string, error) {
	return "", ErrPortalUnavailable
}

// StripePortalCreator hits the real Stripe /v1/billing_portal/sessions
// endpoint. Same plain-stdlib HTTPS approach as the Checkout creator
// — no Stripe SDK.
type StripePortalCreator struct {
	apiKey     string
	apiBase    string
	httpClient *http.Client
}

// StripePortalOption mirrors the checkout-creator option pattern.
type StripePortalOption func(*StripePortalCreator)

// WithPortalAPIBase overrides the Stripe API base URL (tests only).
func WithPortalAPIBase(base string) StripePortalOption {
	return func(s *StripePortalCreator) { s.apiBase = strings.TrimRight(base, "/") }
}

// WithPortalHTTPClient injects a custom http.Client.
func WithPortalHTTPClient(c *http.Client) StripePortalOption {
	return func(s *StripePortalCreator) { s.httpClient = c }
}

// NewStripePortalCreator constructs a real Stripe-backed portal
// creator. Returns an error when apiKey is empty.
func NewStripePortalCreator(apiKey string, opts ...StripePortalOption) (*StripePortalCreator, error) {
	if apiKey == "" {
		return nil, errors.New("billing: NewStripePortalCreator requires apiKey")
	}
	c := &StripePortalCreator{
		apiKey:     apiKey,
		apiBase:    stripeAPIBase,
		httpClient: &http.Client{Timeout: 15 * time.Second},
	}
	for _, opt := range opts {
		opt(c)
	}
	return c, nil
}

// Name implements PortalCreator.
func (s *StripePortalCreator) Name() string { return "stripe" }

// Create implements PortalCreator. Single POST to
// /v1/billing_portal/sessions; returns the hosted-portal URL.
func (s *StripePortalCreator) Create(ctx context.Context, customerID, returnURL string) (string, error) {
	if customerID == "" {
		// Caller should have short-circuited with a 409 before
		// reaching us; returning unavailable here gives the
		// handler defence-in-depth.
		return "", ErrPortalUnavailable
	}
	if returnURL == "" {
		return "", errors.New("billing: portal Create requires returnURL")
	}
	form := url.Values{}
	form.Set("customer", customerID)
	form.Set("return_url", returnURL)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.apiBase+"/v1/billing_portal/sessions", strings.NewReader(form.Encode()))
	if err != nil {
		return "", fmt.Errorf("billing: build portal request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	req.SetBasicAuth(s.apiKey, "")

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("billing: portal request failed: %w", err)
	}
	defer resp.Body.Close()
	respBody, readErr := io.ReadAll(resp.Body)
	if readErr != nil {
		return "", fmt.Errorf("billing: read portal response: %w", readErr)
	}
	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		return "", fmt.Errorf("%w: stripe rejected credentials (%d): %s", ErrPortalUnavailable, resp.StatusCode, truncateForLog(respBody))
	}
	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("billing: stripe portal %d: %s", resp.StatusCode, truncateForLog(respBody))
	}
	var decoded struct {
		URL   string `json:"url"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(respBody, &decoded); err != nil {
		return "", fmt.Errorf("billing: decode portal response: %w", err)
	}
	if decoded.Error != nil {
		return "", fmt.Errorf("billing: stripe portal rejected: %s", decoded.Error.Message)
	}
	if decoded.URL == "" {
		return "", errors.New("billing: stripe portal returned empty url")
	}
	return decoded.URL, nil
}
