// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.
//
// Tests for AppCheckVerifier (2026-06-22 audit P2.B). We sign tokens
// with an in-process RSA key, stand up a fake JWKS endpoint that
// publishes the public half in standard JWKS format, and run the
// verifier against the result. The signature path is exercised end
// to end; iss/aud/exp validation each get a focused negative test.

package account

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

func newAppCheckTestServer(t *testing.T, kid string, key *rsa.PublicKey) *httptest.Server {
	t.Helper()
	n := base64.RawURLEncoding.EncodeToString(key.N.Bytes())
	// Encode exponent big-endian, trim leading zero bytes.
	eb := []byte{
		byte(key.E >> 24), byte(key.E >> 16), byte(key.E >> 8), byte(key.E),
	}
	for len(eb) > 1 && eb[0] == 0 {
		eb = eb[1:]
	}
	e := base64.RawURLEncoding.EncodeToString(eb)
	doc := map[string]any{
		"keys": []map[string]any{{
			"kty": "RSA",
			"alg": "RS256",
			"use": "sig",
			"kid": kid,
			"n":   n,
			"e":   e,
		}},
	}
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(doc)
	}))
}

func signAppCheckToken(t *testing.T, key *rsa.PrivateKey, kid string, claims jwt.MapClaims) string {
	t.Helper()
	tok := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
	tok.Header["kid"] = kid
	signed, err := tok.SignedString(key)
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	return signed
}

// newVerifierForTest constructs an AppCheckVerifier wired to the fake
// JWKS server and overrides the JWKS URL so we don't hit the real
// firebaseappcheck.googleapis.com endpoint.
func newVerifierForTest(t *testing.T, srvURL string, cfg AppCheckConfig) *AppCheckVerifier {
	t.Helper()
	if cfg.HTTPClient == nil {
		cfg.HTTPClient = http.DefaultClient
	}
	v := &AppCheckVerifier{
		cfg:  cfg,
		keys: newJWKSJSONCache(srvURL, cfg.HTTPClient),
	}
	return v
}

func TestAppCheckVerify_Happy(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("genkey: %v", err)
	}
	srv := newAppCheckTestServer(t, "kid-happy", &key.PublicKey)
	defer srv.Close()
	v := newVerifierForTest(t, srv.URL, AppCheckConfig{ProjectNumber: "12345"})

	tok := signAppCheckToken(t, key, "kid-happy", jwt.MapClaims{
		"iss": "https://firebaseappcheck.googleapis.com/12345",
		"aud": []string{"projects/12345"},
		"sub": "1:12345:web:deadbeef",
		"exp": time.Now().Add(10 * time.Minute).Unix(),
		"iat": time.Now().Unix(),
	})
	appID, err := v.Verify(context.Background(), tok)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if appID != "1:12345:web:deadbeef" {
		t.Fatalf("appID = %q", appID)
	}
}

func TestAppCheckVerify_BadIssuer(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	srv := newAppCheckTestServer(t, "kid-iss", &key.PublicKey)
	defer srv.Close()
	v := newVerifierForTest(t, srv.URL, AppCheckConfig{ProjectNumber: "12345"})

	tok := signAppCheckToken(t, key, "kid-iss", jwt.MapClaims{
		"iss": "https://firebaseappcheck.googleapis.com/99999",
		"aud": "projects/12345",
		"exp": time.Now().Add(10 * time.Minute).Unix(),
	})
	if _, err := v.Verify(context.Background(), tok); err == nil ||
		!strings.Contains(err.Error(), "unexpected iss") {
		t.Fatalf("want unexpected iss error, got %v", err)
	}
}

func TestAppCheckVerify_BadAudience(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	srv := newAppCheckTestServer(t, "kid-aud", &key.PublicKey)
	defer srv.Close()
	v := newVerifierForTest(t, srv.URL, AppCheckConfig{ProjectNumber: "12345"})

	tok := signAppCheckToken(t, key, "kid-aud", jwt.MapClaims{
		"iss": "https://firebaseappcheck.googleapis.com/12345",
		"aud": "projects/wrong-id",
		"exp": time.Now().Add(10 * time.Minute).Unix(),
	})
	if _, err := v.Verify(context.Background(), tok); err == nil ||
		!strings.Contains(err.Error(), "aud") {
		t.Fatalf("want aud error, got %v", err)
	}
}

func TestAppCheckVerify_Expired(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	srv := newAppCheckTestServer(t, "kid-exp", &key.PublicKey)
	defer srv.Close()
	v := newVerifierForTest(t, srv.URL, AppCheckConfig{ProjectNumber: "12345"})

	tok := signAppCheckToken(t, key, "kid-exp", jwt.MapClaims{
		"iss": "https://firebaseappcheck.googleapis.com/12345",
		"aud": "projects/12345",
		"exp": time.Now().Add(-1 * time.Hour).Unix(),
	})
	if _, err := v.Verify(context.Background(), tok); err == nil {
		t.Fatalf("want exp error, got nil")
	}
}

func TestAppCheckVerify_UnknownKid(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	srv := newAppCheckTestServer(t, "kid-known", &key.PublicKey)
	defer srv.Close()
	v := newVerifierForTest(t, srv.URL, AppCheckConfig{ProjectNumber: "12345"})

	tok := signAppCheckToken(t, key, "kid-unknown", jwt.MapClaims{
		"iss": "https://firebaseappcheck.googleapis.com/12345",
		"aud": "projects/12345",
		"exp": time.Now().Add(10 * time.Minute).Unix(),
	})
	if _, err := v.Verify(context.Background(), tok); err == nil ||
		!strings.Contains(err.Error(), "kid") {
		t.Fatalf("want missing kid error, got %v", err)
	}
}

func TestAppCheckVerify_AudAcceptsProjectID(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	srv := newAppCheckTestServer(t, "kid-pid", &key.PublicKey)
	defer srv.Close()
	v := newVerifierForTest(t, srv.URL, AppCheckConfig{
		ProjectNumber: "12345",
		ProjectID:     "my-project",
	})
	tok := signAppCheckToken(t, key, "kid-pid", jwt.MapClaims{
		"iss": "https://firebaseappcheck.googleapis.com/12345",
		"aud": "projects/my-project",
		"exp": time.Now().Add(10 * time.Minute).Unix(),
	})
	if _, err := v.Verify(context.Background(), tok); err != nil {
		t.Fatalf("want project-id aud accepted, got %v", err)
	}
}

func TestNewAppCheckVerifier_EmptyConfig(t *testing.T) {
	if _, err := NewAppCheckVerifier(AppCheckConfig{}); err == nil {
		t.Fatal("want error on empty config")
	}
}
