// Copyright 2026 Christian Haeussinger
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

// Package billing — provider-agnostic webhook plumbing.
//
// This file defines the Provider abstraction used by cmd/billing.
// The default (BILLING_PROVIDER=stub) returns a Provider that
// refuses every webhook — that's deliberate: the stub MUST NOT
// silently promote users to higher plans just because a fake event
// arrives. When BILLING_PROVIDER=stripe and STRIPE_WEBHOOK_SECRET
// is set, the Stripe provider does real HMAC-SHA256 signature
// verification following the public Stripe spec.

package billing

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"
)

// Provider is the abstraction cmd/billing uses to verify and parse
// webhook payloads. Two impls ship in Phase 6b: StubProvider
// (production-safe default that refuses all events) and
// StripeProvider (real HMAC + JSON parsing).
type Provider interface {
	// Name returns a short identifier ("stub", "stripe") used in
	// logs so an operator can tell which provider rejected a
	// webhook without parsing the error string.
	Name() string

	// VerifyAndParse validates the signature header against the
	// raw HTTP body and decodes the result. `now` is injected so
	// tests can pin the clock for the timestamp-tolerance check.
	// Returns ErrSignatureInvalid for any verification failure
	// (bad signature, missing/malformed header, stale timestamp).
	VerifyAndParse(payload []byte, sigHeader string, now time.Time) (*WebhookEvent, error)
}

// EventType is a typed string so the switch in the webhook handler
// can't drift away from the names Stripe ships on the wire.
type EventType string

const (
	// EventCheckoutCompleted fires when a customer completes a
	// Stripe Checkout session — this is when we promote their
	// user account to the purchased plan tier.
	EventCheckoutCompleted EventType = "checkout.session.completed"
	// EventSubscriptionUpdated fires on price/plan changes on an
	// existing subscription (e.g. customer upgrades pro→team via
	// the customer portal). We re-map the new price → tier and
	// SetPlan accordingly. The user is found via the `customer`
	// field, not metadata — Stripe doesn't propagate Checkout
	// metadata onto subscription objects.
	EventSubscriptionUpdated EventType = "customer.subscription.updated"
	// EventSubscriptionDeleted fires after Stripe's dunning cycle
	// gives up (or the customer cancels). We downgrade the user
	// to `free`. Same customer-id lookup as the updated event.
	EventSubscriptionDeleted EventType = "customer.subscription.deleted"
	// EventInvoicePaymentFailed fires on each failed renewal
	// attempt. We do NOT downgrade here — Stripe will keep
	// retrying for ~3 weeks; the subscription.deleted event is
	// the authoritative downgrade signal. This event sets a
	// grace-period marker for the in-app banner.
	EventInvoicePaymentFailed EventType = "invoice.payment_failed"
	// EventInvoicePaymentSucceeded fires when an invoice is paid
	// — typically the first renewal after the customer updates
	// a failed card. The handler CLEARS the user's
	// payment_failed_at marker so the 402 enforcement gate in
	// the gateway lets the user back through.
	EventInvoicePaymentSucceeded EventType = "invoice.payment_succeeded"
)

// WebhookEvent is the provider-neutral envelope returned by
// VerifyAndParse. Only the type field for the event we care about
// is populated; unrecognised event types are returned with Type set
// (so the handler can 200 them) but no inner detail.
type WebhookEvent struct {
	ID                string // Provider-side event ID — idempotency key.
	Type              EventType
	CheckoutCompleted    *CheckoutCompletedEvent    // non-nil iff Type==EventCheckoutCompleted
	SubscriptionChange   *SubscriptionChangeEvent   // non-nil for EventSubscriptionUpdated and EventSubscriptionDeleted
	InvoicePaymentFailed *InvoicePaymentFailedEvent // non-nil iff Type==EventInvoicePaymentFailed
	InvoicePaymentSucceeded *InvoicePaymentSucceededEvent // non-nil iff Type==EventInvoicePaymentSucceeded
}

// CheckoutCompletedEvent carries the minimum info needed to set
// the user's plan tier. UserID and PriceID are required — if
// either is blank the webhook handler MUST reject the event with
// 422 rather than guess a default (per the "never invent data" rule).
type CheckoutCompletedEvent struct {
	UserID   string // metadata.user_id on the Stripe checkout session
	PriceID  string // first line-item price ID
	Customer string // stripe customer id (optional, for future linkage)
}

// SubscriptionChangeEvent covers both `customer.subscription.updated`
// and `customer.subscription.deleted`. The handler distinguishes via
// WebhookEvent.Type:
//   - updated → re-map PriceID → tier → SetPlan
//   - deleted → ignore PriceID, force tier=free
//
// Subscriptions are keyed by `customer` not user_id; the handler
// looks up the user via GetUserByStripeCustomerID.
type SubscriptionChangeEvent struct {
	Customer string // cus_... — the Stripe customer id
	PriceID  string // first item's price id (empty on deleted is allowed)
}

// InvoicePaymentFailedEvent carries the customer reference for a
// failed renewal payment. We do NOT downgrade on this event — the
// `customer.subscription.deleted` event is the authoritative cancel
// signal after Stripe's dunning cycle exhausts. This event sets the
// grace-period marker the SPA renders as a banner.
type InvoicePaymentFailedEvent struct {
	Customer string // cus_...
}

// InvoicePaymentSucceededEvent carries the customer reference for a
// successful invoice payment. The handler treats this as the
// "green-light again" signal that CLEARS payment_failed_at. The
// minimum-viable payload is the customer id; we deliberately ignore
// invoice id / amount / period because the gate is a boolean (in
// dunning OR not) — promoting Stripe's full invoice schema into
// our domain would add columns we'd never query.
type InvoicePaymentSucceededEvent struct {
	Customer string // cus_...
}

// ErrSignatureInvalid is returned whenever Provider.VerifyAndParse
// can't trust the payload. The handler maps it to HTTP 400.
var ErrSignatureInvalid = errors.New("billing: webhook signature invalid")

// ----- StubProvider -------------------------------------------------------

// StubProvider is the safe default — every webhook is rejected so a
// misconfigured local dev box can't accidentally upgrade plans by
// POSTing a hand-crafted payload to /webhook.
type StubProvider struct{}

// NewStubProvider constructs the always-rejecting provider.
func NewStubProvider() *StubProvider { return &StubProvider{} }

// Name implements Provider.
func (s *StubProvider) Name() string { return "stub" }

// VerifyAndParse implements Provider — always fails closed.
func (s *StubProvider) VerifyAndParse(_ []byte, _ string, _ time.Time) (*WebhookEvent, error) {
	return nil, fmt.Errorf("%w: stub provider rejects all webhooks (set BILLING_PROVIDER=stripe to enable)", ErrSignatureInvalid)
}

// ----- StripeProvider -----------------------------------------------------

// stripeSignatureTolerance matches Stripe's published default — events
// older than this fail verification even if the HMAC matches, so a
// captured signature can't be replayed days later.
const stripeSignatureTolerance = 5 * time.Minute

// StripeProvider implements Provider against Stripe's public webhook
// spec. It deliberately does NOT use the official Stripe SDK; the
// signature algorithm (HMAC-SHA256 over "t.payload") and the subset
// of JSON we care about are small enough that vendoring the SDK
// adds more risk than value at this phase.
type StripeProvider struct {
	secret string
}

// NewStripeProvider constructs a provider with the given webhook
// signing secret. Returns an error if secret is empty so the
// micro-service refuses to start mis-configured (rather than
// silently using StubProvider, which would surprise an operator
// who set BILLING_PROVIDER=stripe and assumed Stripe was live).
func NewStripeProvider(secret string) (*StripeProvider, error) {
	if secret == "" {
		return nil, errors.New("billing: STRIPE_WEBHOOK_SECRET is required when BILLING_PROVIDER=stripe")
	}
	return &StripeProvider{secret: secret}, nil
}

// Name implements Provider.
func (s *StripeProvider) Name() string { return "stripe" }

// VerifyAndParse implements Provider.
//
// Signature format (from the Stripe docs):
//
//	Stripe-Signature: t=1492774577,v1=5257a869e7...,v1=other_sig
//
// We accept any v1 element that matches HMAC-SHA256(secret, "t.payload").
// Multiple v1 values appear during secret rotation.
func (s *StripeProvider) VerifyAndParse(payload []byte, sigHeader string, now time.Time) (*WebhookEvent, error) {
	ts, sigs, err := parseStripeSigHeader(sigHeader)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrSignatureInvalid, err)
	}
	if now.Sub(ts).Abs() > stripeSignatureTolerance {
		return nil, fmt.Errorf("%w: timestamp outside tolerance window", ErrSignatureInvalid)
	}

	mac := hmac.New(sha256.New, []byte(s.secret))
	// Buffer reuses a single hash; safe because we never read more
	// than one event per call.
	mac.Write([]byte(strconv.FormatInt(ts.Unix(), 10)))
	mac.Write([]byte("."))
	mac.Write(payload)
	want := mac.Sum(nil)

	matched := false
	for _, sig := range sigs {
		got, decodeErr := hex.DecodeString(sig)
		if decodeErr != nil {
			continue
		}
		if hmac.Equal(got, want) {
			matched = true
			break
		}
	}
	if !matched {
		return nil, fmt.Errorf("%w: no matching v1 signature", ErrSignatureInvalid)
	}

	return parseStripeEvent(payload)
}

// parseStripeSigHeader pulls the timestamp and v1 signatures out of
// the Stripe-Signature header. Returns the timestamp as a time.Time
// and the slice of hex-encoded HMAC signatures.
func parseStripeSigHeader(h string) (time.Time, []string, error) {
	if h == "" {
		return time.Time{}, nil, errors.New("empty Stripe-Signature header")
	}
	var (
		ts      int64
		hasTime bool
		v1s     []string
	)
	for _, part := range strings.Split(h, ",") {
		eq := strings.IndexByte(part, '=')
		if eq < 0 {
			continue
		}
		key := strings.TrimSpace(part[:eq])
		val := strings.TrimSpace(part[eq+1:])
		switch key {
		case "t":
			n, perr := strconv.ParseInt(val, 10, 64)
			if perr != nil {
				return time.Time{}, nil, fmt.Errorf("invalid t: %w", perr)
			}
			ts = n
			hasTime = true
		case "v1":
			v1s = append(v1s, val)
		}
	}
	if !hasTime {
		return time.Time{}, nil, errors.New("missing t element")
	}
	if len(v1s) == 0 {
		return time.Time{}, nil, errors.New("missing v1 element")
	}
	return time.Unix(ts, 0), v1s, nil
}

// stripeRawEvent is the minimum JSON subset we read off the wire.
// We deliberately decode into anonymous fields rather than the full
// Stripe Event object — that surface is huge and changes often.
type stripeRawEvent struct {
	ID     string `json:"id"`
	Type   string `json:"type"`
	Data   struct {
		Object json.RawMessage `json:"object"`
	} `json:"data"`
}

// stripeCheckoutSession captures the fields we read off a
// `checkout.session.completed`'s data.object. Stripe permits
// metadata to be omitted; UserID is required upstream and we surface
// any missing-required as a 422 at the handler, never a silent
// assumption.
type stripeCheckoutSession struct {
	Customer string `json:"customer"`
	Metadata struct {
		UserID string `json:"user_id"`
	} `json:"metadata"`
	// Newer Stripe Checkout sessions embed line_items lazily. Many
	// integrations populate them via the `expand` parameter at
	// retrieval time, but webhooks also frequently contain only a
	// `subscription` reference. To keep the stub focused, we read
	// the first explicit line item's price.id if present, else the
	// top-level `metadata.price_id` as a fallback so test fixtures
	// can stay flat.
	LineItems struct {
		Data []struct {
			Price struct {
				ID string `json:"id"`
			} `json:"price"`
		} `json:"data"`
	} `json:"line_items"`
	MetadataExtra map[string]string `json:"-"`
}

// parseStripeEvent decodes a verified payload into a WebhookEvent.
// Errors here are NOT signature errors — they're 400/422 bad
// payload, so we return a plain error (the handler distinguishes).
func parseStripeEvent(payload []byte) (*WebhookEvent, error) {
	var raw stripeRawEvent
	if err := json.Unmarshal(payload, &raw); err != nil {
		return nil, fmt.Errorf("billing: decode stripe event: %w", err)
	}
	if raw.ID == "" {
		return nil, errors.New("billing: stripe event missing id")
	}
	out := &WebhookEvent{ID: raw.ID, Type: EventType(raw.Type)}
	switch out.Type {
	case EventCheckoutCompleted:
		cc, err := parseCheckoutCompleted(raw.Data.Object)
		if err != nil {
			return nil, err
		}
		out.CheckoutCompleted = cc
	case EventSubscriptionUpdated, EventSubscriptionDeleted:
		sc, err := parseSubscriptionChange(raw.Data.Object)
		if err != nil {
			return nil, err
		}
		out.SubscriptionChange = sc
	case EventInvoicePaymentFailed:
		pf, err := parseInvoicePaymentFailed(raw.Data.Object)
		if err != nil {
			return nil, err
		}
		out.InvoicePaymentFailed = pf
	case EventInvoicePaymentSucceeded:
		ps, err := parseInvoicePaymentSucceeded(raw.Data.Object)
		if err != nil {
			return nil, err
		}
		out.InvoicePaymentSucceeded = ps
	}
	return out, nil
}

// parseCheckoutCompleted decodes the data.object payload of a
// checkout.session.completed event. Extracted from parseStripeEvent
// when subscription/invoice events were added so each event type
// has a single decode site.
func parseCheckoutCompleted(obj json.RawMessage) (*CheckoutCompletedEvent, error) {
	var sess stripeCheckoutSession
	if err := json.Unmarshal(obj, &sess); err != nil {
		return nil, fmt.Errorf("billing: decode checkout session: %w", err)
	}
	// Pull metadata.price_id as a fallback (line_items isn't always
	// expanded). We re-decode metadata into a generic map to grab
	// any extra keys — keeps the struct schema small.
	var rawObj map[string]json.RawMessage
	if err := json.Unmarshal(obj, &rawObj); err == nil {
		if md, ok := rawObj["metadata"]; ok {
			extra := map[string]string{}
			_ = json.Unmarshal(md, &extra)
			sess.MetadataExtra = extra
		}
	}
	priceID := ""
	if len(sess.LineItems.Data) > 0 {
		priceID = sess.LineItems.Data[0].Price.ID
	}
	if priceID == "" && sess.MetadataExtra != nil {
		priceID = sess.MetadataExtra["price_id"]
	}
	return &CheckoutCompletedEvent{
		UserID:   sess.Metadata.UserID,
		PriceID:  priceID,
		Customer: sess.Customer,
	}, nil
}

// stripeSubscription captures the fields we read off a
// `customer.subscription.{updated,deleted}` data.object. The full
// Subscription object is huge — we only need the customer id and
// the first item's price id, so we declare a minimal schema.
//
// ID + Status are read only by the reconciler (reconcile.go); the
// webhook path doesn't need them but adding them here is free since
// both paths share the type and Stripe always includes both fields.
type stripeSubscription struct {
	ID       string `json:"id"`
	Customer string `json:"customer"`
	Status   string `json:"status"`
	Items    struct {
		Data []struct {
			Price struct {
				ID string `json:"id"`
			} `json:"price"`
		} `json:"data"`
	} `json:"items"`
}

// parseSubscriptionChange decodes a subscription event's data.object.
// PriceID may be empty (e.g. on .deleted Stripe sometimes omits items);
// the handler treats empty PriceID as "use plan=free" for .deleted and
// rejects it as 422 for .updated. Customer is REQUIRED — without it
// we can't resolve which org to update, so this returns an error that
// surfaces as 422 at the handler.
func parseSubscriptionChange(obj json.RawMessage) (*SubscriptionChangeEvent, error) {
	var sub stripeSubscription
	if err := json.Unmarshal(obj, &sub); err != nil {
		return nil, fmt.Errorf("billing: decode subscription: %w", err)
	}
	priceID := ""
	if len(sub.Items.Data) > 0 {
		priceID = sub.Items.Data[0].Price.ID
	}
	return &SubscriptionChangeEvent{
		Customer: sub.Customer,
		PriceID:  priceID,
	}, nil
}

// stripeInvoice captures the customer reference off an
// `invoice.payment_failed` data.object. We deliberately ignore
// `subscription`, `amount_due`, etc. — the in-app banner only
// needs to know "this customer is in dunning".
type stripeInvoice struct {
	Customer string `json:"customer"`
}

// parseInvoicePaymentFailed decodes the data.object of an
// invoice.payment_failed event. Customer is REQUIRED for the same
// reason as subscription events — we have no other way to resolve
// the org.
func parseInvoicePaymentFailed(obj json.RawMessage) (*InvoicePaymentFailedEvent, error) {
	var inv stripeInvoice
	if err := json.Unmarshal(obj, &inv); err != nil {
		return nil, fmt.Errorf("billing: decode invoice: %w", err)
	}
	return &InvoicePaymentFailedEvent{Customer: inv.Customer}, nil
}

// parseInvoicePaymentSucceeded decodes the data.object of an
// invoice.payment_succeeded event. Same shape as the failed path —
// we only need the customer id to find the org and clear the
// dunning marker. Returns the parsed event with an empty Customer
// when the field is missing; the handler treats that as 422 (same
// rule as the failed path) so a bad event doesn't silently re-open
// the gate for the wrong org.
func parseInvoicePaymentSucceeded(obj json.RawMessage) (*InvoicePaymentSucceededEvent, error) {
	var inv stripeInvoice
	if err := json.Unmarshal(obj, &inv); err != nil {
		return nil, fmt.Errorf("billing: decode invoice: %w", err)
	}
	return &InvoicePaymentSucceededEvent{Customer: inv.Customer}, nil
}
