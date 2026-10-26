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

package billing

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"testing"
	"time"
)

// signStripePayload reproduces Stripe's signing scheme for test
// fixtures so we can verify our verifier against a known-good input.
// Mirrors VerifyAndParse's algorithm in reverse.
func signStripePayload(t *testing.T, secret string, ts time.Time, payload []byte) string {
	t.Helper()
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(strconv.FormatInt(ts.Unix(), 10)))
	mac.Write([]byte("."))
	mac.Write(payload)
	return fmt.Sprintf("t=%d,v1=%s", ts.Unix(), hex.EncodeToString(mac.Sum(nil)))
}

// --- StubProvider ---------------------------------------------------------

func TestStubProvider_AlwaysRejects(t *testing.T) {
	p := NewStubProvider()
	if p.Name() != "stub" {
		t.Errorf("want name=stub, got %q", p.Name())
	}
	_, err := p.VerifyAndParse([]byte(`{"id":"evt_1"}`), "t=1,v1=deadbeef", time.Now())
	if !errors.Is(err, ErrSignatureInvalid) {
		t.Fatalf("stub must reject all payloads with ErrSignatureInvalid, got %v", err)
	}
}

// --- StripeProvider constructor ------------------------------------------

func TestNewStripeProvider_RejectsEmptySecret(t *testing.T) {
	if _, err := NewStripeProvider(""); err == nil {
		t.Fatal("expected error for empty secret")
	}
}

func TestStripeProvider_Name(t *testing.T) {
	p, err := NewStripeProvider("whsec_test")
	if err != nil {
		t.Fatalf("ctor: %v", err)
	}
	if p.Name() != "stripe" {
		t.Errorf("want name=stripe, got %q", p.Name())
	}
}

// --- StripeProvider.VerifyAndParse ---------------------------------------

const testSecret = "whsec_test_secret"

const sampleCheckoutPayload = `{
  "id": "evt_test_123",
  "type": "checkout.session.completed",
  "data": {
    "object": {
      "customer": "cus_abc",
      "metadata": {"user_id": "acme", "price_id": "price_pro_monthly"}
    }
  }
}`

func TestStripeProvider_HappyPath(t *testing.T) {
	p, _ := NewStripeProvider(testSecret)
	now := time.Unix(1_700_000_000, 0)
	header := signStripePayload(t, testSecret, now, []byte(sampleCheckoutPayload))

	ev, err := p.VerifyAndParse([]byte(sampleCheckoutPayload), header, now)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	if ev.ID != "evt_test_123" {
		t.Errorf("event id mismatch: %q", ev.ID)
	}
	if ev.Type != EventCheckoutCompleted {
		t.Errorf("event type mismatch: %q", ev.Type)
	}
	if ev.CheckoutCompleted == nil {
		t.Fatalf("CheckoutCompleted must be populated for the matching type")
	}
	if ev.CheckoutCompleted.UserID != "acme" {
		t.Errorf("user_id mismatch: %q", ev.CheckoutCompleted.UserID)
	}
	if ev.CheckoutCompleted.PriceID != "price_pro_monthly" {
		t.Errorf("price_id (metadata fallback) mismatch: %q", ev.CheckoutCompleted.PriceID)
	}
	if ev.CheckoutCompleted.Customer != "cus_abc" {
		t.Errorf("customer mismatch: %q", ev.CheckoutCompleted.Customer)
	}
}

func TestStripeProvider_LineItemsPriceWins(t *testing.T) {
	// When both line_items and metadata.price_id are present, the
	// real line item wins — metadata is the fallback only.
	payload := `{
      "id": "evt_2",
      "type": "checkout.session.completed",
      "data": {"object": {
        "customer": "cus_x",
        "metadata": {"user_id": "acme", "price_id": "price_fallback"},
        "line_items": {"data": [{"price": {"id": "price_real"}}]}
      }}
    }`
	p, _ := NewStripeProvider(testSecret)
	now := time.Unix(1_700_000_500, 0)
	header := signStripePayload(t, testSecret, now, []byte(payload))

	ev, err := p.VerifyAndParse([]byte(payload), header, now)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	if ev.CheckoutCompleted.PriceID != "price_real" {
		t.Errorf("line_items.price.id must override metadata.price_id, got %q", ev.CheckoutCompleted.PriceID)
	}
}

func TestStripeProvider_UnknownEventTypePassesThrough(t *testing.T) {
	// We acknowledge unrecognised types with a valid envelope (so
	// the handler 200s them — Stripe stops re-delivering) but no
	// inner detail.
	payload := `{"id":"evt_3","type":"customer.created","data":{"object":{}}}`
	p, _ := NewStripeProvider(testSecret)
	now := time.Unix(1_700_001_000, 0)
	header := signStripePayload(t, testSecret, now, []byte(payload))

	ev, err := p.VerifyAndParse([]byte(payload), header, now)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	if ev.Type != "customer.created" {
		t.Errorf("type pass-through failed: %q", ev.Type)
	}
	if ev.CheckoutCompleted != nil {
		t.Errorf("CheckoutCompleted must remain nil for non-checkout events")
	}
}

func TestStripeProvider_BadSignatureRejected(t *testing.T) {
	p, _ := NewStripeProvider(testSecret)
	now := time.Unix(1_700_000_000, 0)
	// Sign with a DIFFERENT secret — should fail verification.
	header := signStripePayload(t, "different_secret", now, []byte(sampleCheckoutPayload))

	_, err := p.VerifyAndParse([]byte(sampleCheckoutPayload), header, now)
	if !errors.Is(err, ErrSignatureInvalid) {
		t.Fatalf("want ErrSignatureInvalid for bad signature, got %v", err)
	}
}

func TestStripeProvider_StaleTimestampRejected(t *testing.T) {
	p, _ := NewStripeProvider(testSecret)
	signedAt := time.Unix(1_700_000_000, 0)
	header := signStripePayload(t, testSecret, signedAt, []byte(sampleCheckoutPayload))

	// "now" is 10 minutes after the signed timestamp — outside the
	// 5-minute tolerance window.
	now := signedAt.Add(10 * time.Minute)
	_, err := p.VerifyAndParse([]byte(sampleCheckoutPayload), header, now)
	if !errors.Is(err, ErrSignatureInvalid) {
		t.Fatalf("want ErrSignatureInvalid for stale signature, got %v", err)
	}
	if err == nil || !strings.Contains(err.Error(), "tolerance") {
		t.Errorf("expected error message to mention tolerance, got: %v", err)
	}
}

func TestStripeProvider_FutureTimestampRejected(t *testing.T) {
	// Symmetry check: a far-future timestamp is just as invalid as
	// a far-past one (defends against replay across clock skew).
	p, _ := NewStripeProvider(testSecret)
	signedAt := time.Unix(1_700_000_000, 0)
	header := signStripePayload(t, testSecret, signedAt, []byte(sampleCheckoutPayload))

	now := signedAt.Add(-10 * time.Minute)
	_, err := p.VerifyAndParse([]byte(sampleCheckoutPayload), header, now)
	if !errors.Is(err, ErrSignatureInvalid) {
		t.Fatalf("want ErrSignatureInvalid for future signature, got %v", err)
	}
}

func TestStripeProvider_MalformedHeaderRejected(t *testing.T) {
	p, _ := NewStripeProvider(testSecret)
	now := time.Unix(1_700_000_000, 0)

	cases := []string{
		"",                              // empty
		"v1=deadbeef",                   // no t
		"t=1700000000",                  // no v1
		"t=notanumber,v1=deadbeef",      // bad t
		"garbage",                       // no = at all
	}
	for _, h := range cases {
		t.Run(h, func(t *testing.T) {
			_, err := p.VerifyAndParse([]byte(sampleCheckoutPayload), h, now)
			if !errors.Is(err, ErrSignatureInvalid) {
				t.Fatalf("header %q: want ErrSignatureInvalid, got %v", h, err)
			}
		})
	}
}

func TestStripeProvider_MultipleV1SignaturesAccepted(t *testing.T) {
	// During secret rotation Stripe sends multiple v1 signatures.
	// We must accept the event if ANY of them matches our secret.
	p, _ := NewStripeProvider(testSecret)
	now := time.Unix(1_700_000_000, 0)
	good := signStripePayload(t, testSecret, now, []byte(sampleCheckoutPayload))
	// Build a header with a bogus v1 first, then the real one.
	header := good + ",v1=00deadbeef"

	ev, err := p.VerifyAndParse([]byte(sampleCheckoutPayload), header, now)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	if ev.ID != "evt_test_123" {
		t.Errorf("event id mismatch: %q", ev.ID)
	}
}

func TestStripeProvider_BadPayloadAfterValidSig(t *testing.T) {
	// Signature verifies (we sign whatever bytes we send), but the
	// JSON is malformed. This is NOT a signature error — it's a
	// plain decode error so the handler can distinguish (400 vs.
	// 422). We assert the error is NOT ErrSignatureInvalid.
	p, _ := NewStripeProvider(testSecret)
	now := time.Unix(1_700_000_000, 0)
	payload := []byte(`{not valid json`)
	header := signStripePayload(t, testSecret, now, payload)

	_, err := p.VerifyAndParse(payload, header, now)
	if err == nil {
		t.Fatal("expected decode error")
	}
	if errors.Is(err, ErrSignatureInvalid) {
		t.Errorf("decode error must NOT be classified as ErrSignatureInvalid: %v", err)
	}
}

func TestStripeProvider_EventMissingIDRejected(t *testing.T) {
	p, _ := NewStripeProvider(testSecret)
	now := time.Unix(1_700_000_000, 0)
	// Valid JSON but no `id` field — we can't dedup without it, so
	// the parser must reject (not silently accept and risk
	// double-processing).
	payload := []byte(`{"type":"checkout.session.completed","data":{"object":{}}}`)
	header := signStripePayload(t, testSecret, now, payload)

	_, err := p.VerifyAndParse(payload, header, now)
	if err == nil {
		t.Fatal("expected error for missing event id")
	}
	if errors.Is(err, ErrSignatureInvalid) {
		t.Errorf("missing-id is a payload error, not a sig error: %v", err)
	}
}

// --- B2: subscription + invoice events ---------------------------------

func TestStripeProvider_SubscriptionUpdatedParsed(t *testing.T) {
	payload := `{
      "id": "evt_sub_1",
      "type": "customer.subscription.updated",
      "data": {"object": {
        "id": "sub_abc",
        "customer": "cus_acme",
        "items": {"data": [{"price": {"id": "price_team_monthly"}}]}
      }}
    }`
	p, _ := NewStripeProvider(testSecret)
	now := time.Unix(1_700_002_000, 0)
	header := signStripePayload(t, testSecret, now, []byte(payload))

	ev, err := p.VerifyAndParse([]byte(payload), header, now)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	if ev.Type != EventSubscriptionUpdated {
		t.Errorf("event type mismatch: %q", ev.Type)
	}
	if ev.SubscriptionChange == nil {
		t.Fatalf("SubscriptionChange must be populated")
	}
	if ev.SubscriptionChange.Customer != "cus_acme" {
		t.Errorf("customer mismatch: %q", ev.SubscriptionChange.Customer)
	}
	if ev.SubscriptionChange.PriceID != "price_team_monthly" {
		t.Errorf("price_id mismatch: %q", ev.SubscriptionChange.PriceID)
	}
	// The checkout-specific field must remain nil so the handler
	// can't accidentally read it for a non-checkout event.
	if ev.CheckoutCompleted != nil {
		t.Errorf("CheckoutCompleted must be nil for subscription events")
	}
}

func TestStripeProvider_SubscriptionDeletedParsed(t *testing.T) {
	// .deleted carries the same Subscription shape; items may or
	// may not be present. We accept empty items because the
	// handler doesn't need a price for the cancellation path.
	payload := `{
      "id": "evt_sub_del_1",
      "type": "customer.subscription.deleted",
      "data": {"object": {
        "id": "sub_abc",
        "customer": "cus_acme",
        "items": {"data": []}
      }}
    }`
	p, _ := NewStripeProvider(testSecret)
	now := time.Unix(1_700_002_500, 0)
	header := signStripePayload(t, testSecret, now, []byte(payload))

	ev, err := p.VerifyAndParse([]byte(payload), header, now)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	if ev.Type != EventSubscriptionDeleted {
		t.Errorf("event type mismatch: %q", ev.Type)
	}
	if ev.SubscriptionChange == nil {
		t.Fatalf("SubscriptionChange must be populated even when items is empty")
	}
	if ev.SubscriptionChange.Customer != "cus_acme" {
		t.Errorf("customer mismatch: %q", ev.SubscriptionChange.Customer)
	}
	if ev.SubscriptionChange.PriceID != "" {
		t.Errorf("PriceID must be empty when items.data is empty, got %q", ev.SubscriptionChange.PriceID)
	}
}

func TestStripeProvider_InvoicePaymentFailedParsed(t *testing.T) {
	payload := `{
      "id": "evt_inv_fail_1",
      "type": "invoice.payment_failed",
      "data": {"object": {
        "id": "in_xyz",
        "customer": "cus_acme",
        "amount_due": 9900
      }}
    }`
	p, _ := NewStripeProvider(testSecret)
	now := time.Unix(1_700_003_000, 0)
	header := signStripePayload(t, testSecret, now, []byte(payload))

	ev, err := p.VerifyAndParse([]byte(payload), header, now)
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	if ev.Type != EventInvoicePaymentFailed {
		t.Errorf("event type mismatch: %q", ev.Type)
	}
	if ev.InvoicePaymentFailed == nil {
		t.Fatalf("InvoicePaymentFailed must be populated")
	}
	if ev.InvoicePaymentFailed.Customer != "cus_acme" {
		t.Errorf("customer mismatch: %q", ev.InvoicePaymentFailed.Customer)
	}
	if ev.SubscriptionChange != nil {
		t.Errorf("SubscriptionChange must be nil for invoice events")
	}
}
