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

package billing

import (
	"errors"
	"testing"
	"time"
)

func TestStripeVerifyAndParse_ExtremeTimestampNoOverflow(t *testing.T) {
	secret := "whsec_test_r3"
	p, err := NewStripeProvider(secret)
	if err != nil {
		t.Fatalf("NewStripeProvider: %v", err)
	}
	payload := []byte(`{"id":"evt_1","type":"invoice.payment_failed","data":{"object":{"customer":"cus_1"}}}`)
	now := time.Unix(1700000000, 0)

	// Extreme timestamps (> 290 years from now) overflowed time.Duration.Abs()
	// prior to R3 because now.Sub(ts) clamped at math.MinInt64 and Abs() stayed negative.
	for _, extremeTS := range []int64{99999999999999, -99999999999999} {
		sig := signStripePayload(t, secret, time.Unix(extremeTS, 0), payload)
		_, verifyErr := p.VerifyAndParse(payload, sig, now)
		if !errors.Is(verifyErr, ErrSignatureInvalid) {
			t.Fatalf("extreme timestamp %d: expected ErrSignatureInvalid, got %v", extremeTS, verifyErr)
		}
	}
}

func TestStripeVerifyAndParse_NullDataObjectRejected(t *testing.T) {
	secret := "whsec_test_r3"
	p, err := NewStripeProvider(secret)
	if err != nil {
		t.Fatalf("NewStripeProvider: %v", err)
	}
	now := time.Unix(1700000000, 0)

	for _, evType := range []string{
		"checkout.session.completed",
		"customer.subscription.updated",
		"customer.subscription.deleted",
		"invoice.payment_failed",
		"invoice.payment_succeeded",
	} {
		for _, body := range []string{
			`{"id":"evt_null","type":"` + evType + `","data":{"object":null}}`,
			`{"id":"evt_missing","type":"` + evType + `","data":{}}`,
		} {
			sig := signStripePayload(t, secret, now, []byte(body))
			_, parseErr := p.VerifyAndParse([]byte(body), sig, now)
			if parseErr == nil {
				t.Errorf("%s (%s): expected error on missing/null data.object, got nil", evType, body)
			}
		}
	}
}

func TestStripeParseCheckoutCompleted_MetadataPriceID(t *testing.T) {
	payload := []byte(`{
		"id": "evt_cc",
		"type": "checkout.session.completed",
		"data": {
			"object": {
				"customer": "cus_123",
				"metadata": {
					"user_id": "usr_456",
					"price_id": "price_pro_monthly",
					"extra_num": 42
				}
			}
		}
	}`)
	ev, err := parseStripeEvent(payload)
	if err != nil {
		t.Fatalf("parseStripeEvent failed: %v", err)
	}
	if ev.CheckoutCompleted == nil || ev.CheckoutCompleted.PriceID != "price_pro_monthly" || ev.CheckoutCompleted.UserID != "usr_456" {
		t.Fatalf("unexpected CheckoutCompleted: %+v", ev.CheckoutCompleted)
	}
}
