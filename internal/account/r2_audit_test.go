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

package account

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

func TestR2_ParseMaxAgeRejectsZeroAndNegative(t *testing.T) {
	for _, bad := range []string{"max-age=0", "max-age=-10", "public, max-age=0", "no-store"} {
		if d, ok := parseMaxAge(bad); ok || d != 0 {
			t.Errorf("parseMaxAge(%q) = (%v, %v), want (0, false)", bad, d, ok)
		}
	}
	if d, ok := parseMaxAge("public, max-age=300"); !ok || d != 300*time.Second {
		t.Fatalf("parseMaxAge(300) = (%v, %v), want (5m, true)", d, ok)
	}
}

func TestR2_AppCheckIssuerValidatedWhenProjectNumberEmpty(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("genkey: %v", err)
	}
	srv := newAppCheckTestServer(t, "kid-empty-proj", &key.PublicKey)
	defer srv.Close()

	// ProjectNumber is intentionally empty (""); issuer prefix MUST still be enforced.
	v := newVerifierForTest(t, srv.URL, AppCheckConfig{ProjectID: "my-proj", ProjectNumber: ""})

	badTok := signAppCheckToken(t, key, "kid-empty-proj", jwt.MapClaims{
		"iss": "https://evil.example.com/12345",
		"aud": "projects/my-proj",
		"sub": "1:12345:web:abc",
		"exp": time.Now().Add(10 * time.Minute).Unix(),
	})
	if _, err := v.Verify(context.Background(), badTok); err == nil || !strings.Contains(err.Error(), "unexpected iss") {
		t.Fatalf("expected non-Firebase iss to be rejected even when ProjectNumber is empty, got %v", err)
	}

	goodTok := signAppCheckToken(t, key, "kid-empty-proj", jwt.MapClaims{
		"iss": "https://firebaseappcheck.googleapis.com/12345",
		"aud": "projects/my-proj",
		"sub": "1:12345:web:abc",
		"exp": time.Now().Add(10 * time.Minute).Unix(),
	})
	if appID, err := v.Verify(context.Background(), goodTok); err != nil || appID != "1:12345:web:abc" {
		t.Fatalf("expected valid Firebase AppCheck iss to succeed, got appID=%q err=%v", appID, err)
	}
}

func TestR2_JWKSJSONRejectsOversizedExponentAndThrottlesUnknownKidBursts(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("genkey: %v", err)
	}

	t.Run("Rejects >4-byte RSA exponent instead of silently overflowing int", func(t *testing.T) {
		n := base64.RawURLEncoding.EncodeToString(key.N.Bytes())
		// 9-byte exponent (>4 bytes) would overflow 64-bit signed int without length guard.
		hugeExp := base64.RawURLEncoding.EncodeToString([]byte{1, 2, 3, 4, 5, 6, 7, 8, 9})
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"keys": []map[string]any{{
					"kty": "RSA",
					"kid": "kid-bad-exp",
					"n":   n,
					"e":   hugeExp,
				}},
			})
		}))
		defer srv.Close()

		cache := newJWKSJSONCache(srv.URL, srv.Client())
		if _, err := cache.get(context.Background(), "kid-bad-exp"); err == nil || !strings.Contains(err.Error(), "invalid exponent") {
			t.Fatalf("expected invalid exponent error, got %v", err)
		}
	})

	t.Run("Cooldown prevents unknown-kid bursts from hammering JWKS endpoint", func(t *testing.T) {
		var fetches atomic.Int32
		n := base64.RawURLEncoding.EncodeToString(key.N.Bytes())
		e := base64.RawURLEncoding.EncodeToString([]byte{1, 0, 1}) // 65537
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			fetches.Add(1)
			w.Header().Set("Cache-Control", "public, max-age=3600")
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"keys": []map[string]any{{
					"kty": "RSA",
					"kid": "kid-real",
					"n":   n,
					"e":   e,
				}},
			})
		}))
		defer srv.Close()

		cache := newJWKSJSONCache(srv.URL, srv.Client())
		// Initial warm-up fetch:
		if _, err := cache.get(context.Background(), "kid-real"); err != nil {
			t.Fatalf("warm-up get: %v", err)
		}
		if got := fetches.Load(); got != 1 {
			t.Fatalf("expected 1 warm-up fetch, got %d", got)
		}

		// 20 forged unknown kids within jwksMinRefreshInterval MUST NOT trigger outbound HTTP calls.
		for i := 0; i < 20; i++ {
			if _, err := cache.get(context.Background(), "forged-kid"); err == nil {
				t.Fatal("expected error for forged kid")
			}
		}
		if got := fetches.Load(); got != 1 {
			t.Fatalf("expected cooldown to cap JWKS fetches at 1 during unknown-kid burst, got %d", got)
		}
	})
}

type fakeAccountRowR2 struct {
	u   User
	err error
}

func (r fakeAccountRowR2) Scan(dest ...any) error {
	if r.err != nil {
		return r.err
	}
	*(dest[0].(*string)) = r.u.ID
	*(dest[1].(*string)) = r.u.Email
	*(dest[2].(*string)) = r.u.DisplayName
	*(dest[3].(*string)) = r.u.Timezone
	*(dest[4].(*string)) = r.u.StripeCustomerID
	*(dest[5].(*string)) = string(r.u.PlanTier)
	*(dest[6].(**time.Time)) = r.u.PaymentFailedAt
	*(dest[7].(*bool)) = r.u.IsSuperadmin
	*(dest[8].(*time.Time)) = r.u.CreatedAt
	*(dest[9].(**time.Time)) = r.u.DeletedAt
	*(dest[10].(**time.Time)) = r.u.TokensInvalidAfter
	*(dest[11].(*string)) = string(r.u.Status)
	*(dest[12].(**time.Time)) = r.u.ApprovedAt
	*(dest[13].(*string)) = r.u.ApprovedBy
	return nil
}

type fakeSoftDeletedUserConnR2 struct {
	softUser User
}

func (f fakeSoftDeletedUserConnR2) Exec(context.Context, string, ...any) (pgconn.CommandTag, error) {
	return pgconn.NewCommandTag("OK 0"), nil
}

func (f fakeSoftDeletedUserConnR2) Query(context.Context, string, ...any) (pgx.Rows, error) {
	return nil, nil
}

func (f fakeSoftDeletedUserConnR2) QueryRow(_ context.Context, sql string, _ ...any) pgx.Row {
	switch {
	case strings.Contains(sql, "INSERT INTO users"):
		return fakeAccountRowR2{err: &pgconn.PgError{Code: "23505", Message: "duplicate key value violates unique constraint \"users_pkey\""}}
	case strings.Contains(sql, "deleted_at IS NULL"):
		return fakeAccountRowR2{err: pgx.ErrNoRows}
	default:
		return fakeAccountRowR2{u: f.softUser}
	}
}

func TestR2_StoreCreateUserReturnsErrAccountDeletedOnSoftDeletedUID(t *testing.T) {
	delAt := time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)
	store := NewStore(fakeSoftDeletedUserConnR2{
		softUser: User{
			ID:        "uid-soft-deleted",
			Email:     "deleted@example.com",
			PlanTier:  PlanFree,
			Status:    StatusApproved,
			CreatedAt: delAt.Add(-time.Hour),
			DeletedAt: &delAt,
		},
	})

	_, created, err := store.CreateUser(context.Background(), "uid-soft-deleted", "deleted@example.com", "Ghost")
	if created {
		t.Fatal("expected created=false for soft-deleted UID")
	}
	if !errors.Is(err, ErrAccountDeleted) {
		t.Fatalf("expected ErrAccountDeleted (not ErrEmailTaken), got %v", err)
	}
}
