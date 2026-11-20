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
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"io"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}

func newTestFirebaseResolver(t *testing.T, projectID string, fixedNow time.Time) (*FirebaseResolver, *rsa.PrivateKey, string) {
	t.Helper()

	privKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("rsa.GenerateKey: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "test-firebase"},
		NotBefore:    fixedNow.Add(-time.Hour),
		NotAfter:     fixedNow.Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
	}
	certDER, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &privKey.PublicKey, privKey)
	if err != nil {
		t.Fatalf("x509.CreateCertificate: %v", err)
	}
	certPEM := string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certDER}))

	const kid = "test-kid-1"
	jwksBody, err := json.Marshal(map[string]string{kid: certPEM})
	if err != nil {
		t.Fatalf("marshal jwks: %v", err)
	}

	client := &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.URL.String() != firebaseProductionCertsURL {
				return &http.Response{
					StatusCode: http.StatusNotFound,
					Body:       io.NopCloser(strings.NewReader("not found")),
					Header:     make(http.Header),
				}, nil
			}
			hdr := make(http.Header)
			hdr.Set("Content-Type", "application/json")
			hdr.Set("Cache-Control", "public, max-age=3600")
			return &http.Response{
				StatusCode: http.StatusOK,
				Body:       io.NopCloser(strings.NewReader(string(jwksBody))),
				Header:     hdr,
			}, nil
		}),
	}

	resolver, err := NewFirebaseResolver(FirebaseConfig{
		ProjectID:  projectID,
		HTTPClient: client,
		Now:        func() time.Time { return fixedNow },
		Lookup: func(_ context.Context, uid string) (User, error) {
			return User{
				ID:       uid,
				Email:    uid + "@example.com",
				PlanTier: PlanPro,
				Status:   StatusApproved,
			}, nil
		},
	})
	if err != nil {
		t.Fatalf("NewFirebaseResolver: %v", err)
	}
	return resolver, privKey, kid
}

func signTestJWT(t *testing.T, key *rsa.PrivateKey, kid string, claims jwt.MapClaims) string {
	t.Helper()
	tok := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
	if kid != "" {
		tok.Header["kid"] = kid
	}
	signed, err := tok.SignedString(key)
	if err != nil {
		t.Fatalf("SignedString: %v", err)
	}
	return signed
}

func TestT5_R10_FirebaseResolver_XUserAuthorizationAndOIDCCoexistence(t *testing.T) {
	const projectID = "rehearsal-test-proj"
	now := time.Date(2026, 11, 19, 12, 0, 0, 0, time.UTC)
	resolver, firebaseKey, kid := newTestFirebaseResolver(t, projectID, now)

	oidcKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("rsa.GenerateKey oidc: %v", err)
	}

	firebaseToken := signTestJWT(t, firebaseKey, kid, jwt.MapClaims{
		"iss":   firebaseSecureTokenIssuer + projectID,
		"aud":   projectID,
		"sub":   "user-alice",
		"email": "user-alice@example.com",
		"iat":   now.Add(-time.Minute).Unix(),
		"exp":   now.Add(time.Hour).Unix(),
	})

	oidcToken := signTestJWT(t, oidcKey, "oidc-kid-1", jwt.MapClaims{
		"iss":   "https://accounts.google.com",
		"aud":   "https://gateway-xyz.run.app",
		"sub":   "sa-12345",
		"email": "runner-sa@proj.iam.gserviceaccount.com",
		"iat":   now.Add(-time.Minute).Unix(),
		"exp":   now.Add(time.Hour).Unix(),
	})

	t.Run("X-User-Authorization overrides Cloud Run OIDC Authorization header", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodPost, "/api/v1/spawn", nil)
		req.Header.Set("Authorization", "Bearer "+oidcToken)
		req.Header.Set("X-User-Authorization", "Bearer "+firebaseToken)

		uc, err := resolver.Resolve(req)
		if err != nil {
			t.Fatalf("Resolve failed: %v", err)
		}
		if uc.UserID != "user-alice" || uc.NeedsInit {
			t.Fatalf("unexpected UserContext: %+v", uc)
		}
	})

	t.Run("Authorization OIDC falls back to __session cookie when X-User-Authorization is absent", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodPost, "/api/v1/spawn", nil)
		req.Header.Set("Authorization", "Bearer "+oidcToken)
		req.AddCookie(&http.Cookie{Name: "__session", Value: firebaseToken})

		uc, err := resolver.Resolve(req)
		if err != nil {
			t.Fatalf("Resolve failed: %v", err)
		}
		if uc.UserID != "user-alice" || uc.NeedsInit {
			t.Fatalf("unexpected UserContext: %+v", uc)
		}
	})

	t.Run("Invalid non-OIDC Authorization does NOT silently fall back to __session cookie", func(t *testing.T) {
		forgedFirebase := signTestJWT(t, oidcKey, kid, jwt.MapClaims{
			"iss":   firebaseSecureTokenIssuer + projectID,
			"aud":   projectID,
			"sub":   "attacker",
			"email": "attacker@example.com",
			"iat":   now.Add(-time.Minute).Unix(),
			"exp":   now.Add(time.Hour).Unix(),
		})
		req := httptest.NewRequest(http.MethodPost, "/api/v1/spawn", nil)
		req.Header.Set("Authorization", "Bearer "+forgedFirebase)
		req.AddCookie(&http.Cookie{Name: "__session", Value: firebaseToken})

		if _, err := resolver.Resolve(req); err == nil {
			t.Fatal("expected forged Authorization token to fail instead of falling back to __session")
		}
	})

	t.Run("Malformed X-User-Authorization fails closed without falling back to Authorization or __session", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodPost, "/api/v1/spawn", nil)
		req.Header.Set("X-User-Authorization", "Bearer bad;token")
		req.Header.Set("Authorization", "Bearer "+firebaseToken)
		req.AddCookie(&http.Cookie{Name: "__session", Value: firebaseToken})

		if _, err := resolver.Resolve(req); !errors.Is(err, ErrNoIdentity) {
			t.Fatalf("expected ErrNoIdentity for malformed X-User-Authorization, got %v", err)
		}
	})

	t.Run("Legacy Authorization and __session-only paths remain unchanged", func(t *testing.T) {
		reqAuth := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
		reqAuth.Header.Set("Authorization", "Bearer "+firebaseToken)
		uc1, err := resolver.Resolve(reqAuth)
		if err != nil || uc1.UserID != "user-alice" {
			t.Fatalf("Authorization-only failed: uc=%+v err=%v", uc1, err)
		}

		reqCookie := httptest.NewRequest(http.MethodGet, "/ws", nil)
		reqCookie.AddCookie(&http.Cookie{Name: "__session", Value: firebaseToken})
		uc2, err := resolver.Resolve(reqCookie)
		if err != nil || uc2.UserID != "user-alice" {
			t.Fatalf("__session-only failed: uc=%+v err=%v", uc2, err)
		}
	})
}
