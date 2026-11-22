// Copyright 2026 Christian Haeussinger. Apache-2.0.

package account

import (
	"context"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/auth"
	"github.com/golang-jwt/jwt/v5"
)

// firebaseSecureTokenIssuer is the issuer prefix Google sets on every
// Firebase ID token. The full issuer is
// "https://securetoken.google.com/<project_id>".
const firebaseSecureTokenIssuer = "https://securetoken.google.com/"

// firebaseProductionCertsURL is Google's public x509 cert endpoint
// for verifying Firebase ID tokens issued in production.
const firebaseProductionCertsURL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"

// tokenRevokeSkew is the clock-skew grace window applied to the
// `iat`-vs-`tokens_invalid_after` comparison in claimsToUser. Tokens
// whose `iat` is within this window before the revoke watermark are
// still accepted. NTP drift on GCP hosts is sub-second so this is
// effectively a safety margin against the user-device clock running a
// hair fast at the moment a logout request reached the server.
//
// Security cost: a token stolen immediately before logout gains at
// most this much extra usable lifetime. The next refresh (≤1h later)
// is unaffected because the fresh token's `iat` is well past the
// watermark. See 2026-06-21 audit P3 #7.
const tokenRevokeSkew = 2 * time.Second

// UserLookup is the read-side hook the resolver calls to convert a
// verified Firebase UID into a fully-populated UserContext. *Store's
// GetUser satisfies this exactly; tests pass a fake.
//
// Returning ErrNotFound is the explicit signal that the user has
// authenticated with Firebase but has never called /api/v1/me/init —
// the resolver translates it to UserContext{NeedsInit:true} so the
// gateway can allow-list that single endpoint.
type UserLookup func(ctx context.Context, uid string) (User, error)

// FirebaseConfig configures a FirebaseResolver.
type FirebaseConfig struct {
	// ProjectID is the GCP project ID hosting the Firebase project.
	// Used as the JWT audience and (suffixed) issuer. Mandatory.
	ProjectID string

	// EmulatorHost is the host:port of the Firebase Auth emulator,
	// e.g. "127.0.0.1:9099". When non-empty the resolver accepts
	// unsigned ("alg": "none") tokens and skips JWKS fetching. MUST
	// be empty in production.
	EmulatorHost string

	// HTTPClient is used to fetch JWKS. Defaults to
	// http.DefaultClient.
	HTTPClient *http.Client

	// Now returns the current time. Defaults to time.Now. Tests
	// inject a fake clock.
	Now func() time.Time

	// Lookup converts a verified UID to a User. Optional; when nil
	// the resolver returns NeedsInit=true for every authenticated
	// user (useful in tests). In production this points at
	// Store.GetUser.
	Lookup UserLookup
}

// FirebaseResolver is a Resolver that verifies Firebase ID tokens
// supplied via X-User-Authorization: Bearer ..., Authorization: Bearer ...,
// or the __session cookie.
// On success it looks up the users row and returns a populated
// UserContext; on ErrNotFound from Lookup it returns
// UserContext{NeedsInit:true}.
type FirebaseResolver struct {
	cfg   FirebaseConfig
	keys  *jwksCache
	allow bool // emulator allow-unsigned mode
}

// NewFirebaseResolver constructs a FirebaseResolver.
func NewFirebaseResolver(cfg FirebaseConfig) (*FirebaseResolver, error) {
	cfg.ProjectID = strings.TrimSpace(cfg.ProjectID)
	cfg.EmulatorHost = strings.TrimSpace(cfg.EmulatorHost)
	if cfg.ProjectID == "" {
		return nil, errors.New("account: FirebaseConfig.ProjectID required")
	}
	if cfg.EmulatorHost != "" && isGCPRuntime() {
		return nil, errors.New("account: FirebaseConfig.EmulatorHost is forbidden in GCP runtime")
	}
	if cfg.HTTPClient == nil {
		cfg.HTTPClient = &http.Client{Timeout: 10 * time.Second}
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	r := &FirebaseResolver{cfg: cfg, allow: cfg.EmulatorHost != ""}
	if !r.allow {
		r.keys = newJWKSCache(firebaseProductionCertsURL, cfg.HTTPClient, cfg.Now)
	}
	return r, nil
}

// WithLookup attaches a UserLookup after construction. Useful when
// the *Store is built after the resolver (typical gateway startup
// order). Returns the receiver so callers can chain.
func (f *FirebaseResolver) WithLookup(fn UserLookup) *FirebaseResolver {
	if f == nil {
		return nil
	}
	f.cfg.Lookup = fn
	return f
}

// Resolve implements Resolver.
func (f *FirebaseResolver) Resolve(r *http.Request) (UserContext, error) {
	if f == nil {
		return UserContext{}, errors.New("account: nil FirebaseResolver")
	}
	if r == nil {
		return UserContext{}, ErrNoIdentity
	}
	raw := extractBearer(r)
	if raw == "" {
		return UserContext{}, ErrNoIdentity
	}
	// Cloud Run IAM coexistence: when X-User-Authorization is absent and
	// Authorization carries an ingress Google OIDC token (not a Firebase ID
	// token), verify the __session cookie if present.
	if r.Header.Get("X-User-Authorization") == "" && r.Header.Get("Authorization") != "" && IsGoogleOIDCToken(raw) {
		if cookieTok := extractSessionCookie(r); cookieTok != "" {
			raw = cookieTok
		}
	}
	claims, err := f.verify(r.Context(), raw)
	if err != nil {
		return UserContext{}, fmt.Errorf("account: verify firebase id token: %w", err)
	}
	return f.claimsToUser(r.Context(), claims)
}

func (f *FirebaseResolver) now() time.Time {
	if f != nil && f.cfg.Now != nil {
		return f.cfg.Now()
	}
	return time.Now()
}

// claimsToUser converts verified Firebase claims into a UserContext.
//
// Behavior:
//   - sub missing                       -> error (defensive; Firebase always sets sub)
//   - Lookup nil                        -> NeedsInit=true (test mode)
//   - Lookup returns ErrNotFound        -> NeedsInit=true (real "first time" flow)
//   - Lookup returns other error        -> propagate (DB outage shouldn't degrade silently)
//   - Lookup returns User (deleted)     -> error (cannot use a soft-deleted account)
//   - Lookup returns User (active)      -> populate from row, prefer DB email over token
func (f *FirebaseResolver) claimsToUser(ctx context.Context, c jwt.MapClaims) (UserContext, error) {
	uid, _ := c["sub"].(string)
	if strings.TrimSpace(uid) == "" {
		uid, _ = c["user_id"].(string)
	}
	if strings.TrimSpace(uid) == "" {
		return UserContext{}, errors.New("account: token missing subject")
	}
	email, _ := c["email"].(string)
	email = strings.ToLower(strings.TrimSpace(email))

	if f.cfg.Lookup == nil {
		return UserContext{UserID: uid, Email: email, NeedsInit: true}, nil
	}
	u, err := f.cfg.Lookup(ctx, uid)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			return UserContext{UserID: uid, Email: email, NeedsInit: true}, nil
		}
		return UserContext{}, fmt.Errorf("account: user lookup: %w", err)
	}
	if !u.IsActive() {
		// P1.A (2026-06-22 audit): surface the deleted state as a
		// distinct sentinel so the auth middleware can emit 410 Gone
		// (instead of the legacy 401, which masked the cause and made
		// the soft-delete + re-sign-in flow look like a generic auth
		// failure). Requires the Lookup to be Store.GetUserAnyState
		// (not Store.GetUser, which filters deleted_at and would
		// collapse "deleted" into ErrNotFound → NeedsInit → PK crash on
		// the retained id row).
		return UserContext{}, ErrAccountDeleted
	}

	// P3 #17: compare token issued-at against the user's
	// tokens_invalid_after watermark. Logout / account-delete bumps the
	// watermark to NOW(); any token minted strictly before that is
	// rejected here. We allow `iat == TokensInvalidAfter` (same second)
	// to avoid spurious failures on a tight relog within the same
	// second; a token minted in the same second as a revoke is
	// definitively post-revoke from the user's wall-clock perspective.
	//
	// P3 #7 (2026-06-21 audit): apply a small clock-skew grace window.
	// NTP drift on GCP hosts is sub-second, so this is effectively never
	// hit in practice — but a token minted on a user's device a few
	// hundred milliseconds before logout could otherwise be rejected
	// when its `iat` is rounded down to the previous second. The grace
	// is measured against the TOKEN's `iat`, not the watermark, so the
	// security cost is bounded: a stolen pre-logout token gains at most
	// `tokenRevokeSkew` of extra usable lifetime, and the next refresh
	// (≤1h later) is unaffected because the new token's `iat` is well
	// after the watermark.
	if u.TokensInvalidAfter != nil {
		if iat, ok := tokenIssuedAt(c); ok {
			if iat.Add(tokenRevokeSkew).Before(*u.TokensInvalidAfter) {
				return UserContext{}, ErrTokenRevoked
			}
		}
		// If the token has no `iat` claim, we cannot prove it post-dates
		// the revoke and must reject — Firebase tokens always carry `iat`,
		// so a missing claim is an injection attempt or a malformed token.
		if _, ok := tokenIssuedAt(c); !ok {
			return UserContext{}, ErrTokenRevoked
		}
	}

	return UserContext{
		UserID:       u.ID,
		Email:        u.Email,
		IsSuperadmin: u.IsSuperadmin,
		NeedsInit:    false,
		Status:       u.Status,
	}, nil
}

// tokenIssuedAt extracts the `iat` claim as a time.Time. JWT spec
// says iat is a NumericDate (seconds since epoch); the jwt library
// hands it back as float64 when going through MapClaims. Returns
// (zero, false) when iat is missing or has the wrong type.
const unixToInternalSeconds int64 = 62135596800

func validUnixSeconds(sec int64) bool {
	return sec >= math.MinInt64+unixToInternalSeconds && sec <= math.MaxInt64-unixToInternalSeconds
}

func tokenIssuedAt(c jwt.MapClaims) (time.Time, bool) {
	switch v := c["iat"].(type) {
	case float64:
		if math.IsNaN(v) || math.IsInf(v, 0) || v < -float64(unixToInternalSeconds) || v > 1e15 {
			return time.Time{}, false
		}
		sec := int64(v)
		if !validUnixSeconds(sec) {
			return time.Time{}, false
		}
		return time.Unix(sec, 0).UTC(), true
	case int64:
		if !validUnixSeconds(v) {
			return time.Time{}, false
		}
		return time.Unix(v, 0).UTC(), true
	case json.Number:
		if n, err := v.Int64(); err == nil && validUnixSeconds(n) {
			return time.Unix(n, 0).UTC(), true
		}
	}
	return time.Time{}, false
}

// IsGoogleOIDCToken reports whether raw is a syntactically valid JWT whose
// unverified issuer is Google Accounts OIDC ("https://accounts.google.com" or
// "accounts.google.com"), such as a Cloud Run IAM service-to-service ID token.
func IsGoogleOIDCToken(raw string) bool {
	parser := jwt.NewParser()
	tok, _, err := parser.ParseUnverified(raw, jwt.MapClaims{})
	if err != nil {
		return false
	}
	claims, ok := tok.Claims.(jwt.MapClaims)
	if !ok {
		return false
	}
	iss, _ := claims["iss"].(string)
	return iss == "https://accounts.google.com" || iss == "accounts.google.com"
}

// extractBearer pulls the JWT out of X-User-Authorization: Bearer ...
// (used when Cloud Run IAM OIDC occupies Authorization),
// Authorization: Bearer ..., or the __session cookie (used for WebSocket
// connections, which can't carry custom headers in browsers). Returns "" if
// none present or if an explicit header is malformed.
func extractBearer(r *http.Request) string {
	if h := r.Header.Get("X-User-Authorization"); h != "" {
		if tok := auth.ParseBearerHeader(h); auth.IsCookieSafeToken(tok) {
			return tok
		}
		return ""
	}
	if h := r.Header.Get("Authorization"); h != "" {
		if tok := auth.ParseBearerHeader(h); auth.IsCookieSafeToken(tok) {
			return tok
		}
		return ""
	}
	return extractSessionCookie(r)
}

func extractSessionCookie(r *http.Request) string {
	if r == nil {
		return ""
	}
	if c, err := r.Cookie("__session"); err == nil && c.Value != "" {
		tok := strings.TrimSpace(c.Value)
		if auth.IsCookieSafeToken(tok) {
			return tok
		}
	}
	return ""
}

// verify parses and verifies a Firebase ID token. In emulator mode
// signature checks are skipped (the emulator issues unsigned
// tokens); in production the token must be signed by Google with
// one of the public keys served from firebaseProductionCertsURL.
func (f *FirebaseResolver) verify(ctx context.Context, raw string) (jwt.MapClaims, error) {
	parser := jwt.NewParser(
		jwt.WithIssuedAt(),
		jwt.WithExpirationRequired(),
		jwt.WithAudience(f.cfg.ProjectID),
		jwt.WithIssuer(firebaseSecureTokenIssuer+f.cfg.ProjectID),
		jwt.WithTimeFunc(f.now),
	)

	if f.allow {
		// Emulator: tokens are unsigned ("alg": "none"). The jwt
		// library disallows "none" by default for safety, so we
		// parse without verification and then validate the claims
		// manually.
		tok, _, err := parser.ParseUnverified(raw, jwt.MapClaims{})
		if err != nil {
			return nil, err
		}
		claims, ok := tok.Claims.(jwt.MapClaims)
		if !ok {
			return nil, errors.New("account: emulator token has unexpected claim shape")
		}
		now := f.now()
		exp, err := claims.GetExpirationTime()
		if err != nil {
			return nil, fmt.Errorf("account: emulator token exp: %w", err)
		}
		if exp == nil || !exp.Time.After(now) {
			return nil, errors.New("account: emulator token expired")
		}
		nbf, err := claims.GetNotBefore()
		if err != nil {
			return nil, fmt.Errorf("account: emulator token nbf: %w", err)
		}
		if nbf != nil && now.Before(nbf.Time) {
			return nil, errors.New("account: emulator token not valid yet")
		}
		iat, err := claims.GetIssuedAt()
		if err != nil {
			return nil, fmt.Errorf("account: emulator token iat: %w", err)
		}
		if iat != nil && iat.Time.After(now) {
			return nil, errors.New("account: emulator token issued in the future")
		}
		iss, _ := claims["iss"].(string)
		if iss != firebaseSecureTokenIssuer+f.cfg.ProjectID {
			return nil, fmt.Errorf("account: emulator token issuer mismatch: %q", iss)
		}
		audList, err := claims.GetAudience()
		if err != nil {
			return nil, fmt.Errorf("account: emulator token aud: %w", err)
		}
		audOK := false
		for _, a := range audList {
			if a == f.cfg.ProjectID {
				audOK = true
				break
			}
		}
		if !audOK {
			return nil, fmt.Errorf("account: emulator token audience mismatch: %v", audList)
		}
		return claims, nil
	}

	if f.keys == nil {
		return nil, errors.New("account: uninitialized FirebaseResolver")
	}
	tok, err := parser.ParseWithClaims(raw, jwt.MapClaims{}, func(t *jwt.Token) (any, error) {
		if _, ok := t.Method.(*jwt.SigningMethodRSA); !ok {
			return nil, fmt.Errorf("account: unexpected JWT alg %v", t.Header["alg"])
		}
		kid, _ := t.Header["kid"].(string)
		if kid == "" {
			return nil, errors.New("account: JWT missing kid header")
		}
		return f.keys.get(ctx, kid)
	})
	if err != nil {
		return nil, err
	}
	claims, ok := tok.Claims.(jwt.MapClaims)
	if !ok {
		return nil, errors.New("account: token has unexpected claim shape")
	}
	return claims, nil
}

// --- JWKS cache ----------------------------------------------------------

type jwksCache struct {
	url    string
	client *http.Client
	now    func() time.Time

	mu        sync.RWMutex
	keys      map[string]*rsa.PublicKey
	fetchedAt time.Time
	expiry    time.Time

	// P2 (2026-06-23 audit): single-flight gate around refresh().
	// Without it, a burst of N requests arriving while keys are
	// expired (or with an unknown kid) each fire a parallel JWKS
	// HTTP fetch — N times the latency, N times the outbound
	// connections, and a thundering-herd hit on Google's JWKS
	// endpoint during cold start. The gate funnels concurrent
	// refreshes into a single network call; followers wait on
	// the same fetch and then re-check the cache.
	refreshMu sync.Mutex
}

func newJWKSCache(url string, client *http.Client, now func() time.Time) *jwksCache {
	if now == nil {
		now = time.Now
	}
	return &jwksCache{url: url, client: client, now: now, keys: map[string]*rsa.PublicKey{}}
}

const jwksMinRefreshInterval = 10 * time.Second

func (c *jwksCache) get(ctx context.Context, kid string) (*rsa.PublicKey, error) {
	now := c.now()
	c.mu.RLock()
	if now.Before(c.expiry) {
		if k, ok := c.keys[kid]; ok {
			c.mu.RUnlock()
			return k, nil
		}
		// Cooldown guard: prevent forged unknown-kid bursts from forcing
		// an outbound JWKS HTTP fetch on every unauthenticated request.
		if len(c.keys) > 0 && !c.fetchedAt.IsZero() && now.Sub(c.fetchedAt) < jwksMinRefreshInterval {
			c.mu.RUnlock()
			return nil, fmt.Errorf("account: unknown JWT kid %q", kid)
		}
	}
	c.mu.RUnlock()

	// Single-flight: only one goroutine refreshes at a time. Once
	// the lock is acquired, re-check the cache before re-fetching —
	// a concurrent goroutine that won the race may have already
	// populated the kid we need.
	c.refreshMu.Lock()
	defer c.refreshMu.Unlock()
	now = c.now()
	c.mu.RLock()
	if now.Before(c.expiry) {
		if k, ok := c.keys[kid]; ok {
			c.mu.RUnlock()
			return k, nil
		}
		if len(c.keys) > 0 && !c.fetchedAt.IsZero() && now.Sub(c.fetchedAt) < jwksMinRefreshInterval {
			c.mu.RUnlock()
			return nil, fmt.Errorf("account: unknown JWT kid %q", kid)
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
	return nil, fmt.Errorf("account: unknown JWT kid %q", kid)
}

func (c *jwksCache) refresh(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.url, nil)
	if err != nil {
		return err
	}
	resp, err := c.client.Do(req)
	if err != nil {
		return fmt.Errorf("account: fetch jwks: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("account: jwks endpoint %s returned %d", c.url, resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return err
	}
	var certs map[string]string
	if err := json.Unmarshal(body, &certs); err != nil {
		return fmt.Errorf("account: parse jwks json: %w", err)
	}
	keys := make(map[string]*rsa.PublicKey, len(certs))
	for kid, certPEM := range certs {
		block, _ := pem.Decode([]byte(certPEM))
		if block == nil {
			return fmt.Errorf("account: jwks kid %s has no PEM block", kid)
		}
		cert, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			return fmt.Errorf("account: jwks kid %s parse cert: %w", kid, err)
		}
		rsaKey, ok := cert.PublicKey.(*rsa.PublicKey)
		if !ok {
			return fmt.Errorf("account: jwks kid %s is not RSA", kid)
		}
		keys[kid] = rsaKey
	}
	if len(keys) == 0 {
		return errors.New("account: jwks endpoint returned 0 keys")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.keys = keys
	c.fetchedAt = c.now()
	expiresIn := time.Hour
	if cc := resp.Header.Get("Cache-Control"); cc != "" {
		if d, ok := parseMaxAge(cc); ok {
			expiresIn = d
			if expiresIn > 24*time.Hour {
				expiresIn = 24 * time.Hour
			}
		}
	}
	c.expiry = c.fetchedAt.Add(expiresIn)
	return nil
}

func parseMaxAge(cc string) (time.Duration, bool) {
	const prefix = "max-age="
	const maxDurationSec = math.MaxInt64 / int64(time.Second)
	for _, raw := range strings.Split(cc, ",") {
		part := strings.TrimSpace(raw)
		if len(part) <= len(prefix) || !strings.EqualFold(part[:len(prefix)], prefix) {
			continue
		}
		sec := strings.TrimSpace(strings.Trim(part[len(prefix):], `"`))
		v, err := strconv.ParseInt(sec, 10, 64)
		if err != nil {
			if errors.Is(err, strconv.ErrRange) && isAllASCIIUnsignedDigits(sec) {
				return time.Duration(math.MaxInt64), true
			}
			return 0, false
		}
		if v <= 0 {
			return 0, false
		}
		if v > maxDurationSec {
			return time.Duration(math.MaxInt64), true
		}
		return time.Duration(v) * time.Second, true
	}
	return 0, false
}

func isAllASCIIUnsignedDigits(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}

// Compile-time guard.
var _ Resolver = (*FirebaseResolver)(nil)
