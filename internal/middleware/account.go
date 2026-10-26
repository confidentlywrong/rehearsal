// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

// Package middleware (account.go) provides the per-user identity
// middleware that replaces the previous multi-org tenant middleware.
//
// In the per-user model there is no org concept and no SAAS_MODE
// flag — every deployment is always Firebase Auth + per-user data.
// Local development uses LocalResolver, which is refused in cloud
// runtimes unless ALLOW_LOCAL_AUTH=1 is explicitly set.
package middleware

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

// AccountOptions configures the Account middleware.
type AccountOptions struct {
	// Resolver to use. Required. Build it via AccountResolverFromEnv.
	Resolver account.Resolver

	// SkipPaths is a list of URL path prefixes that bypass the
	// middleware entirely. Use this for liveness probes, the login
	// page, and any other unauthenticated route. CORS preflight
	// (OPTIONS) requests always bypass.
	SkipPaths []string

	// UnauthorizedHandler customizes the response when a request
	// has no usable identity. If nil, writes 401 JSON
	// {"error":"authentication_required"}.
	UnauthorizedHandler gin.HandlerFunc

	// InitPaths is a list of URL path prefixes that ARE allowed to
	// run with UserContext.NeedsInit=true. Use this for the
	// /api/v1/me/init endpoint (and nothing else). All other paths
	// return via NeedsInitHandler.
	InitPaths []string

	// NeedsInitHandler customizes the response when a request
	// carries a verified Firebase identity that has no users row
	// yet AND the path is not in InitPaths. If nil, writes 409 JSON
	// {"error":"needs_init", "init_url":"/api/v1/me/init"}.
	NeedsInitHandler gin.HandlerFunc

	// DeletedUserCheck is an optional hook that returns true when
	// the given user has been soft-deleted. Wired by main.go to
	// Store.GetUser + IsActive(). Nil disables the check.
	//
	// Errors are fail-open (logged; request proceeds). Token expiry
	// (~1h) caps worst-case leak.
	DeletedUserCheck func(ctx context.Context, userID string) (bool, error)

	// DeletedHandler customizes the response when DeletedUserCheck
	// returns true. If nil, writes 410 Gone JSON
	// {"error":"account_deleted"}.
	DeletedHandler gin.HandlerFunc

	// PaymentFailedCheck returns the most recent payment-failure
	// timestamp for a user (zero = never failed). When non-nil, the
	// middleware gates state-changing requests after the grace
	// period; reads always pass. Nil disables the gate.
	//
	// Errors are fail-open: a DB outage must not DoS healthy
	// writes.
	PaymentFailedCheck func(ctx context.Context, userID string) (time.Time, error)

	// PaymentFailedGracePeriod is the window after a failure during
	// which writes are still allowed. Zero defaults to 7 days
	// (Stripe Smart Retry window). Negative values clamp to zero
	// (gate fires immediately on first failure).
	PaymentFailedGracePeriod time.Duration

	// PaymentFailedExemptPaths are write paths that bypass the gate
	// even after grace expires. Defaults include /api/v1/me and
	// /api/v1/billing so users can always reach the Customer Portal
	// to update their card. Caller-supplied paths EXTEND this list
	// (never replace) so a missing override can't lock users out of
	// billing.
	PaymentFailedExemptPaths []string

	// PaymentFailedHandler customizes the gate response. If nil,
	// writes 402 Payment Required JSON
	// {"error":"payment_failed", "portal_url":"/api/v1/billing/portal",
	//  "grace_expired_at":"..."}.
	PaymentFailedHandler gin.HandlerFunc

	// ApprovedPaths is the allow-list of URL prefixes that pending /
	// rejected users may still reach. Use this for /api/v1/me,
	// /api/v1/me/logout and /api/v1/auth/exchange so the SPA can
	// read its own status and sign out. Matching rules are identical
	// to InitPaths (exact match or "/"-suffix subtree). Approval
	// gate is disabled entirely when this slice is empty (legacy
	// behaviour — no allow-list means no gate).
	ApprovedPaths []string

	// NotApprovedHandler customizes the response when a request
	// from a verified user with a users row reaches a path NOT in
	// ApprovedPaths AND the user's status is not StatusApproved.
	// If nil, writes 403 JSON {"error":"account_pending"} for
	// StatusPending and {"error":"account_rejected"} for
	// StatusRejected. (Manual-approval flow, 2026-06-26.)
	NotApprovedHandler func(c *gin.Context, status account.UserStatus)
}

// Account returns a gin middleware that resolves a UserContext and
// attaches it to the request's context.Context.
//
// Unlike the legacy Tenant middleware there is no SAAS_MODE check —
// this middleware is ALWAYS mounted. Local development uses
// LocalResolver (refused in cloud runtimes unless ALLOW_LOCAL_AUTH=1).
func Account(opts AccountOptions) gin.HandlerFunc {
	if opts.Resolver == nil {
		log.Fatal("account middleware: Resolver is required")
	}
	unauthorized := opts.UnauthorizedHandler
	if unauthorized == nil {
		unauthorized = defaultAccountUnauthorized
	}
	needsInit := opts.NeedsInitHandler
	if needsInit == nil {
		needsInit = defaultNeedsInit
	}
	deleted := opts.DeletedHandler
	if deleted == nil {
		deleted = defaultAccountDeleted
	}
	deletedCheck := opts.DeletedUserCheck
	skips := append([]string{}, opts.SkipPaths...)
	initPaths := append([]string{}, opts.InitPaths...)

	paymentCheck := opts.PaymentFailedCheck
	paymentHandler := opts.PaymentFailedHandler
	if paymentHandler == nil {
		paymentHandler = defaultAccountPaymentFailed
	}
	paymentGrace := opts.PaymentFailedGracePeriod
	if paymentGrace <= 0 {
		paymentGrace = 7 * 24 * time.Hour
	}
	paymentExempt := append([]string{
		"/api/v1/billing",
		"/api/v1/me",
	}, opts.PaymentFailedExemptPaths...)

	approvedPaths := append([]string{}, opts.ApprovedPaths...)
	notApprovedHandler := opts.NotApprovedHandler
	if notApprovedHandler == nil {
		notApprovedHandler = defaultNotApproved
	}

	return func(c *gin.Context) {
		if c.Request.Method == http.MethodOptions {
			c.Next()
			return
		}
		path := c.Request.URL.Path
		for _, p := range skips {
			// Skip entries that already end in "/" are explicit
			// subtree prefixes (e.g. "/__/auth/"); match by
			// HasPrefix as written. Entries without a trailing
			// slash must match the path exactly OR be followed by
			// "/" so "/login" doesn't accidentally skip auth on a
			// future "/logins" route (audit 2026-06-22, B-8).
			if strings.HasSuffix(p, "/") {
				if strings.HasPrefix(path, p) {
					c.Next()
					return
				}
			} else if path == p || strings.HasPrefix(path, p+"/") {
				c.Next()
				return
			}
		}

		uc, err := opts.Resolver.Resolve(c.Request)
		if err != nil {
			log.Printf("ACCOUNT: %s %s rejected: %v",
				c.Request.Method, c.Request.URL.Path, err)
			// P3 #17: surface revoked-token rejections with a distinct
			// error code so the SPA can clear localStorage and bounce
			// to /login instead of silently retrying.
			if errors.Is(err, account.ErrTokenRevoked) {
				c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
					"error": "token_revoked",
					"hint":  "this session was signed out; please sign in again",
				})
				return
			}
			// P1.A (2026-06-22 audit): surface deleted-account rejections
			// as 410 Gone via the configured deleted handler so the SPA
			// can show "account_deleted" and stop retrying. Without this
			// branch a returning soft-deleted user looked like a brand-
			// new signup (NeedsInit=true), crashed on /me/init's PK
			// collision, and got 500 init_failed forever.
			if errors.Is(err, account.ErrAccountDeleted) {
				deleted(c)
				return
			}
			unauthorized(c)
			return
		}

		if deletedCheck != nil && uc.UserID != "" && !uc.NeedsInit {
			isDeleted, derr := deletedCheck(c.Request.Context(), uc.UserID)
			if derr != nil {
				log.Printf("ACCOUNT: deleted-user check failed user=%s: %v", uc.UserID, derr)
			} else if isDeleted {
				deleted(c)
				return
			}
		}

		// Identity verified, but user has no DB row yet: allow the
		// /api/v1/me/init endpoint through and 409 everything else.
		if uc.NeedsInit {
			allowed := false
			for _, p := range initPaths {
				if path == p || strings.HasPrefix(path, p+"/") {
					allowed = true
					break
				}
			}
			c.Set("account", uc)
			c.Request = c.Request.WithContext(account.WithContext(c.Request.Context(), uc))
			if !allowed {
				needsInit(c)
				return
			}
			c.Next()
			return
		}

		c.Set("account", uc)
		c.Request = c.Request.WithContext(account.WithContext(c.Request.Context(), uc))

		// Manual-approval gate (2026-06-26). When ApprovedPaths is
		// configured, pending or rejected users may only reach
		// paths in the allow-list (typically /api/v1/me, /me/logout
		// and /auth/exchange so the SPA can read its own state and
		// sign out). Every other path returns 403 with a distinct
		// error code so the SPA can route to the "Account pending"
		// or "Account rejected" screens. Empty ApprovedPaths
		// disables the gate (legacy installations without the
		// approval columns).
		//
		// SAAS_MODE=off bypass (2026-07-02): single-tenant local
		// deployments (``make start``) do NOT have a manual-approval
		// workflow -- there is no admin to click "approve" and the
		// concept doesn't apply. Skipping the gate entirely when
		// SAAS_MODE is off restores byte-identical legacy behaviour
		// for local dev; the flow only fires under SAAS_MODE=on
		// where a real admin queue exists.
		if len(approvedPaths) > 0 && !uc.IsApproved() && uc.Status != "" && account.SaaSModeEnabled() {
			allowed := false
			for _, p := range approvedPaths {
				if strings.HasSuffix(p, "/") {
					if strings.HasPrefix(path, p) {
						allowed = true
						break
					}
				} else if path == p || strings.HasPrefix(path, p+"/") {
					allowed = true
					break
				}
			}
			if !allowed {
				notApprovedHandler(c, uc.Status)
				return
			}
		}

		// Payment-failed gate. Same fail-open semantics as
		// DeletedUserCheck. Only fires on state-changing methods
		// against non-exempt paths.
		if paymentCheck != nil && uc.UserID != "" && isWriteMethod(c.Request.Method) && !hasPathPrefix(path, paymentExempt) {
			failedAt, perr := paymentCheck(c.Request.Context(), uc.UserID)
			if perr != nil {
				log.Printf("ACCOUNT: payment-failed check errored user=%s: %v", uc.UserID, perr)
			} else if !failedAt.IsZero() && time.Since(failedAt) >= paymentGrace {
				graceExpired := failedAt.Add(paymentGrace)
				c.Set("payment_grace_expired_at", graceExpired)
				paymentHandler(c)
				return
			}
		}

		c.Next()
	}
}

// AccountResolverFromEnv builds an account.Resolver based on
// IDENTITY_PROVIDER:
//
//	local    -> account.LocalResolver  (fixture; default)
//	firebase -> account.FirebaseResolver with FIREBASE_PROJECT_ID +
//	            optional FIREBASE_AUTH_EMULATOR_HOST
//
// The local provider is REFUSED on Cloud Run / GAE / Cloud Functions
// unless ALLOW_LOCAL_AUTH=1 is explicitly set. This protects against
// accidentally shipping the fixture to production.
func AccountResolverFromEnv() (account.Resolver, error) {
	provider := strings.TrimSpace(os.Getenv("IDENTITY_PROVIDER"))
	if provider == "" {
		provider = "local"
	}
	if provider == "local" && looksLikeGCP() && os.Getenv("ALLOW_LOCAL_AUTH") != "1" {
		return nil, errSafetyTrip("IDENTITY_PROVIDER=local is forbidden on Cloud Run; set IDENTITY_PROVIDER=firebase (or ALLOW_LOCAL_AUTH=1 to override)")
	}
	switch provider {
	case "local":
		return account.NewLocalResolver(), nil
	case "firebase":
		projectID := strings.TrimSpace(os.Getenv("FIREBASE_PROJECT_ID"))
		if projectID == "" {
			projectID = strings.TrimSpace(os.Getenv("GOOGLE_CLOUD_PROJECT"))
		}
		if projectID == "" {
			return nil, errSafetyTrip("IDENTITY_PROVIDER=firebase requires FIREBASE_PROJECT_ID (or GOOGLE_CLOUD_PROJECT)")
		}
		emulator := strings.TrimSpace(os.Getenv("FIREBASE_AUTH_EMULATOR_HOST"))
		return account.NewFirebaseResolver(account.FirebaseConfig{
			ProjectID:    projectID,
			EmulatorHost: emulator,
			HTTPClient:   &http.Client{Timeout: 5 * time.Second},
		})
	default:
		return nil, errSafetyTrip("unknown IDENTITY_PROVIDER=" + provider)
	}
}

// RequireSuperadmin returns a middleware that aborts with
// 403 unless the UserContext attached to the request has
// IsSuperadmin=true.
//
// Unlike the legacy tenant.RequireSuperadmin there is no escalation
// hook — IsSuperadmin is a boolean column on the users table and
// the resolver always carries the current value (FirebaseResolver
// reads it from Store.GetUser; LocalResolver reads it from the
// X-Local-Dev-Superadmin header).
//
// TEMPORARY NAME: will be renamed to RequireSuperadmin once the
// legacy tenant middleware is deleted (the two functions can't
// coexist with the same name in the same package).
func RequireSuperadmin() gin.HandlerFunc {
	return func(c *gin.Context) {
		v, ok := c.Get("account")
		if !ok {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
			return
		}
		uc, ok := v.(account.UserContext)
		if !ok || !uc.IsSuperadmin {
			c.AbortWithStatusJSON(http.StatusForbidden, gin.H{"error": "superadmin_required"})
			return
		}
		c.Next()
	}
}

// RequireUser returns a middleware that aborts with 401 when the
// request lacks a UserContext (typically because Account middleware
// wasn't mounted upstream). Use this on routes that need a verified
// user but should never reach the NeedsInit allow-list.
func RequireUser() gin.HandlerFunc {
	return func(c *gin.Context) {
		v, ok := c.Get("account")
		if !ok {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
			return
		}
		uc, ok := v.(account.UserContext)
		if !ok || !uc.HasIdentity() {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
			return
		}
		if uc.NeedsInit {
			c.AbortWithStatusJSON(http.StatusConflict, gin.H{
				"error":    "needs_init",
				"init_url": "/api/v1/me/init",
			})
			return
		}
		c.Next()
	}
}

// AccountFromContext is a convenience wrapper around
// account.FromContext that returns the zero UserContext when
// missing. Handlers that have already gone through RequireUser may
// call this without checking the bool.
func AccountFromContext(ctx context.Context) account.UserContext {
	return account.FromContext(ctx)
}

func defaultAccountUnauthorized(c *gin.Context) {
	c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
		"error": "authentication_required",
	})
}

func defaultNeedsInit(c *gin.Context) {
	c.AbortWithStatusJSON(http.StatusConflict, gin.H{
		"error":    "needs_init",
		"init_url": "/api/v1/me/init",
	})
}

func defaultAccountDeleted(c *gin.Context) {
	c.AbortWithStatusJSON(http.StatusGone, gin.H{
		"error": "account_deleted",
		"hint":  "this account has been removed; contact support if this was unexpected",
	})
}

func defaultAccountPaymentFailed(c *gin.Context) {
	body := gin.H{
		"error":      "payment_failed",
		"portal_url": "/api/v1/billing/portal",
		"hint":       "your subscription payment is past due; update your card via the Customer Portal to resume writes (read access remains available)",
	}
	if v, ok := c.Get("payment_grace_expired_at"); ok {
		if t, ok := v.(time.Time); ok {
			body["grace_expired_at"] = t.UTC().Format(time.RFC3339)
		}
	}
	c.AbortWithStatusJSON(http.StatusPaymentRequired, body)
}

// defaultNotApproved is the manual-approval gate's response when a
// signed-in user with a users row tries to reach a route outside
// ApprovedPaths and is not StatusApproved. The error code distinguishes
// "pending" (operator hasn't reviewed yet — please wait) from
// "rejected" (operator denied — won't change). The SPA routes to
// different screens for each. 403 (rather than 401) so the client
// does not retry the request after refreshing the token.
func defaultNotApproved(c *gin.Context, status account.UserStatus) {
	code := "account_pending"
	hint := "your sign-up is awaiting administrator approval; you'll receive an email when approved"
	if status == account.StatusRejected {
		code = "account_rejected"
		hint = "your sign-up was not approved; contact support if you believe this is in error"
	}
	c.AbortWithStatusJSON(http.StatusForbidden, gin.H{
		"error":  code,
		"status": string(status),
		"hint":   hint,
	})
}
