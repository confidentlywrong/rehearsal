// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

package account

// Firebase App Check server-side verification.
//
// SPA + /login already initialise App Check with the
// ReCaptchaEnterpriseProvider so signin/signup RPCs against Identity
// Platform carry an App Check token. That protects the Firebase Auth
// endpoints, but a stolen Firebase ID token (XSS, leaked dev console,
// etc.) can still be replayed against our gateway via `curl` because
// the gateway only verifies the ID token. P2.B (2026-06-22 audit)
// closes the gap: a separate App Check JWT, attached by the client
// as `X-Firebase-AppCheck: <token>`, is verified here against the
// public JWKS published by `firebaseappcheck.googleapis.com`.
//
// Validation:
//   - alg = RS256 (Firebase App Check is RSA-signed).
//   - exp is required and validated.
//   - iss = "https://firebaseappcheck.googleapis.com/<projectNumber>"
//   - aud contains "projects/<projectNumber>" or "projects/<projectID>"
//     (Firebase emits both; we accept either).
//   - sub (the Firebase App ID) is returned to the caller for audit
//     logging — we do NOT validate it against an allowlist because
//     all apps under the project are equally trusted.
//
// Wiring lives in internal/middleware/appcheck.go. Default mode is
// "off" so existing deploys are unaffected; flip to "report-only"
// via env var first, then "enforce" once logs are clean.

import (
	"context"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// appCheckJWKSURL is the public JWKS endpoint for Firebase App Check
// signing keys. Per Firebase docs the endpoint serves standard JWKS
// (not the x509 PEM dict the ID-token verifier consumes).
const appCheckJWKSURL = "https://firebaseappcheck.googleapis.com/v1/jwks"

// AppCheckConfig drives a server-side App Check token verifier. At
// least one of ProjectNumber / ProjectID must be non-empty; if both
// are set we accept tokens whose aud matches either.
type AppCheckConfig struct {
	// ProjectNumber is the numeric GCP project number. App Check
	// JWTs use it both as the issuer URL suffix and as the canonical
	// aud value ("projects/<number>"). Strongly preferred over
	// ProjectID because Firebase always includes it.
	ProjectNumber string
	// ProjectID is the alphanumeric Firebase project id. Some
	// historical App Check tokens include it as a secondary aud
	// value; accept it as a fallback so we don't reject otherwise
	// valid tokens.
	ProjectID string
	// HTTPClient is used to fetch the JWKS. Defaults to
	// http.DefaultClient. Set a non-default timeout in production
	// to avoid hanging the request pipeline on a JWKS-fetch stall.
	HTTPClient *http.Client
}

// AppCheckVerifier verifies App Check JWTs against the public JWKS.
// Safe for concurrent use.
type AppCheckVerifier struct {
	cfg  AppCheckConfig
	keys *jwksJSONCache
}

// NewAppCheckVerifier returns a ready-to-use verifier. Errors on
// completely-empty config (no project number AND no project id).
func NewAppCheckVerifier(cfg AppCheckConfig) (*AppCheckVerifier, error) {
	if strings.TrimSpace(cfg.ProjectNumber) == "" && strings.TrimSpace(cfg.ProjectID) == "" {
		return nil, errors.New("account: AppCheckConfig needs ProjectNumber or ProjectID")
	}
	if cfg.HTTPClient == nil {
		cfg.HTTPClient = http.DefaultClient
	}
	return &AppCheckVerifier{
		cfg:  cfg,
		keys: newJWKSJSONCache(appCheckJWKSURL, cfg.HTTPClient),
	}, nil
}

// Verify parses and validates the App Check JWT. Returns the `sub`
// claim (Firebase App ID) on success; the caller can log it for
// per-app analytics but should not gate on it (we don't enforce an
// app allowlist server-side).
func (v *AppCheckVerifier) Verify(ctx context.Context, raw string) (appID string, err error) {
	if v == nil {
		return "", errors.New("account: nil AppCheckVerifier")
	}
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", errors.New("account: app check token is empty")
	}
	parser := jwt.NewParser(
		jwt.WithValidMethods([]string{"RS256"}),
		jwt.WithExpirationRequired(),
	)
	tok, err := parser.Parse(raw, func(t *jwt.Token) (any, error) {
		kid, _ := t.Header["kid"].(string)
		if kid == "" {
			return nil, errors.New("account: app check token missing kid")
		}
		return v.keys.get(ctx, kid)
	})
	if err != nil {
		return "", fmt.Errorf("account: parse app check token: %w", err)
	}
	claims, ok := tok.Claims.(jwt.MapClaims)
	if !ok {
		return "", errors.New("account: app check claims wrong shape")
	}
	// iss validation. App Check issuer is the project-scoped URL.
	if v.cfg.ProjectNumber != "" {
		iss, _ := claims["iss"].(string)
		expected := "https://firebaseappcheck.googleapis.com/" + v.cfg.ProjectNumber
		if iss != expected {
			return "", fmt.Errorf("account: app check unexpected iss %q (want %q)", iss, expected)
		}
	}
	// aud validation. May be a single string or an array; accept any
	// member that matches our project number / project id.
	var audVals []string
	switch raw := claims["aud"].(type) {
	case string:
		audVals = []string{raw}
	case []any:
		for _, a := range raw {
			if s, ok := a.(string); ok {
				audVals = append(audVals, s)
			}
		}
	}
	accept := map[string]bool{}
	if v.cfg.ProjectNumber != "" {
		accept["projects/"+v.cfg.ProjectNumber] = true
	}
	if v.cfg.ProjectID != "" {
		accept["projects/"+v.cfg.ProjectID] = true
	}
	audOK := false
	for _, a := range audVals {
		if accept[a] {
			audOK = true
			break
		}
	}
	if !audOK {
		return "", fmt.Errorf("account: app check aud %v not in accept set %v", audVals, keysOf(accept))
	}
	sub, _ := claims["sub"].(string)
	return sub, nil
}

func keysOf(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

// --- JWKS (JSON Web Key Set) cache --------------------------------------
//
// Differs from jwksCache (firebase.go) which expects an x509 PEM
// dict. App Check serves standard JWKS: kty=RSA, n and e are
// base64url-encoded modulus and exponent. We parse them directly
// rather than pull in a third-party JWKS library to keep the
// dependency surface small.

type jwksJSONCache struct {
	url    string
	client *http.Client

	mu     sync.RWMutex
	keys   map[string]*rsa.PublicKey
	expiry time.Time
}

func newJWKSJSONCache(url string, client *http.Client) *jwksJSONCache {
	return &jwksJSONCache{url: url, client: client, keys: map[string]*rsa.PublicKey{}}
}

func (c *jwksJSONCache) get(ctx context.Context, kid string) (*rsa.PublicKey, error) {
	c.mu.RLock()
	if time.Now().Before(c.expiry) {
		if k, ok := c.keys[kid]; ok {
			c.mu.RUnlock()
			return k, nil
		}
	}
	c.mu.RUnlock()
	if err := c.refresh(ctx); err != nil {
		return nil, err
	}
	c.mu.RLock()
	defer c.mu.RUnlock()
	if k, ok := c.keys[kid]; ok {
		return k, nil
	}
	return nil, fmt.Errorf("account: appcheck jwks missing kid %q", kid)
}

func (c *jwksJSONCache) refresh(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.url, nil)
	if err != nil {
		return err
	}
	resp, err := c.client.Do(req)
	if err != nil {
		return fmt.Errorf("account: fetch appcheck jwks: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("account: appcheck jwks endpoint returned %d", resp.StatusCode)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	var doc struct {
		Keys []struct {
			Kty string `json:"kty"`
			Kid string `json:"kid"`
			N   string `json:"n"`
			E   string `json:"e"`
			Alg string `json:"alg"`
		} `json:"keys"`
	}
	if err := json.Unmarshal(body, &doc); err != nil {
		return fmt.Errorf("account: parse appcheck jwks: %w", err)
	}
	next := make(map[string]*rsa.PublicKey, len(doc.Keys))
	for _, k := range doc.Keys {
		if k.Kty != "RSA" || k.Kid == "" {
			continue
		}
		n, err := base64.RawURLEncoding.DecodeString(k.N)
		if err != nil {
			return fmt.Errorf("account: appcheck jwks kid %s bad n: %w", k.Kid, err)
		}
		e, err := base64.RawURLEncoding.DecodeString(k.E)
		if err != nil {
			return fmt.Errorf("account: appcheck jwks kid %s bad e: %w", k.Kid, err)
		}
		eInt := 0
		for _, b := range e {
			eInt = eInt<<8 | int(b)
		}
		if eInt == 0 {
			return fmt.Errorf("account: appcheck jwks kid %s zero exponent", k.Kid)
		}
		next[k.Kid] = &rsa.PublicKey{
			N: new(big.Int).SetBytes(n),
			E: eInt,
		}
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.keys = next
	// parseMaxAge lives in firebase.go (same package).
	expiresIn := time.Hour
	if cc := resp.Header.Get("Cache-Control"); cc != "" {
		if d, ok := parseMaxAge(cc); ok {
			expiresIn = d
			if expiresIn > 24*time.Hour {
				expiresIn = 24 * time.Hour
			}
		}
	}
	c.expiry = time.Now().Add(expiresIn)
	return nil
}
