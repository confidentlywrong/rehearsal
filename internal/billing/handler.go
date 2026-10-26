// Copyright 2026 Christian Haeussinger. Apache-2.0.

// handler.go wires the per-user billing endpoints used by the SPA:
//
//	POST /api/v1/billing/checkout  → opens a Stripe Checkout session
//	GET  /api/v1/usage             → current month token usage + caps
//	GET  /api/v1/plans             → catalog of available plans
//
// All three require an authenticated user (account.UserContext on
// the request context); the middleware in cmd/gateway sets that up
// before mounting these routes.

package billing

import (
	"context"
	"errors"
	"log/slog"
	"net/http"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/logging"
	"github.com/gin-gonic/gin"
)

// UserLookup is the minimum account-store surface the checkout
// handler needs to fetch the live user row.
type UserLookup interface {
	GetUser(ctx context.Context, id string) (account.User, error)
}

// UsageReader is the subset of *Store the admin dashboard's
// per-user usage panel needs: the headline rollup plus the
// agent-level breakdown.
type UsageReader interface {
	GetMonthlyUsage(ctx context.Context, userID, yearMonth string) (MonthlyUsage, error)
	GetAgentBreakdown(ctx context.Context, userID, yearMonth string) ([]AgentBreakdown, error)
}

// PlansLister is the subset of *Store the /api/v1/plans handler
// needs.
type PlansLister interface {
	ListPlans(ctx context.Context) ([]Plan, error)
}

// CheckoutHandler returns a Gin handler that opens a hosted
// Checkout session for the requesting user and returns the URL.
//
// Request body: {"tier": "pro|team|enterprise", "success_url": "...",
// "cancel_url": "..."}.
//
// Response (200): {"url": "https://checkout.stripe.com/..."}.
// 401 if no UserContext; 404 if user row missing; 503 with
// ErrCheckoutUnavailable if the provider isn't configured for the
// requested tier.
//
// allowedBaseURL is the trusted base URL the success/cancel URLs
// must share an origin with (typically STRIPE_RETURN_BASE_URL).
// When empty (stub creator path / tests) the origin check is
// skipped — the stub never actually redirects users.
func CheckoutHandler(users UserLookup, creator CheckoutCreator, allowedBaseURL string) gin.HandlerFunc {
	return func(c *gin.Context) {
		uc, ok := account.FromContextOK(c.Request.Context())
		if !ok || uc.UserID == "" {
			c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
			return
		}
		var req struct {
			Tier       string `json:"tier"`
			SuccessURL string `json:"success_url"`
			CancelURL  string `json:"cancel_url"`
		}
		if err := c.ShouldBindJSON(&req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid body: " + err.Error()})
			return
		}
		tier := account.PlanTier(req.Tier)
		// Reject free (downgrade, not a checkout target) and comp
		// (superadmin-granted; checkout would replace the grant with
		// a paid subscription, defeating the purpose). enterprise is
		// allowed because some deployments sell it self-serve.
		if !tier.IsKnown() || tier == account.PlanFree || tier == account.PlanComp {
			c.JSON(http.StatusBadRequest, gin.H{"error": "tier must be one of pro|team|enterprise"})
			return
		}
		if req.SuccessURL == "" || req.CancelURL == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "success_url and cancel_url required"})
			return
		}
		// Open-redirect guard (MED-1 in the 2026-06-25 audit). Both
		// URLs must share an origin with allowedBaseURL — without
		// this, any signed-in user can craft a Checkout whose
		// success_url is an attacker-controlled phishing page.
		if err := ValidateRedirectURL(req.SuccessURL, allowedBaseURL); err != nil {
			logging.FromContext(c.Request.Context()).Warn("billing.checkout.bad_success_url",
				slog.String("user_id", uc.UserID),
				slog.String("url", req.SuccessURL),
				slog.String("error", err.Error()))
			c.JSON(http.StatusBadRequest, gin.H{"error": "success_url not allowed"})
			return
		}
		if err := ValidateRedirectURL(req.CancelURL, allowedBaseURL); err != nil {
			logging.FromContext(c.Request.Context()).Warn("billing.checkout.bad_cancel_url",
				slog.String("user_id", uc.UserID),
				slog.String("url", req.CancelURL),
				slog.String("error", err.Error()))
			c.JSON(http.StatusBadRequest, gin.H{"error": "cancel_url not allowed"})
			return
		}
		user, err := users.GetUser(c.Request.Context(), uc.UserID)
		if err != nil {
			if errors.Is(err, account.ErrNotFound) {
				c.JSON(http.StatusNotFound, gin.H{"error": "user not found"})
				return
			}
			logging.FromContext(c.Request.Context()).Error("billing.checkout.user_lookup_failed",
				slog.String("user_id", uc.UserID), slog.String("error", err.Error()))
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal"})
			return
		}
		url, err := creator.Create(c.Request.Context(), user, tier, req.SuccessURL, req.CancelURL)
		if err != nil {
			if errors.Is(err, ErrCheckoutUnavailable) {
				c.JSON(http.StatusServiceUnavailable, gin.H{
					"error": "billing not configured", "code": "checkout_unavailable",
				})
				return
			}
			logging.FromContext(c.Request.Context()).Error("billing.checkout.create_failed",
				slog.String("user_id", uc.UserID),
				slog.String("tier", string(tier)),
				slog.String("error", err.Error()))
			c.JSON(http.StatusBadGateway, gin.H{"error": "checkout create failed"})
			return
		}
		c.JSON(http.StatusOK, gin.H{"url": url})
	}
}

// OnDemandDrainer is invoked by UsageHandler before serving the
// /api/v1/usage response so the billing tile always reflects the
// latest Python-side metering rows. Returns the number of rows
// forwarded; errors are logged at the call site and never fail the
// HTTP request — a fresh-read failure must not block the user from
// seeing already-persisted usage data.
//
// *SQLiteDrainer satisfies this interface; deployments where no
// drain step is needed (e.g. METERING_SINK=pubsub, or unit tests)
// pass nil and the handler skips the call.
type OnDemandDrainer interface {
	DrainOnce(ctx context.Context) (int, error)
}

// UsageHandler returns a Gin handler that reports the requesting
// user's current month token usage and cap. 200 with QuotaState on
// success; 401 if unauthenticated.
//
// When `drainer` is non-nil, a single drain pass runs before
// CheckQuota so the response reflects metering rows the Python
// side wrote since the last call. Replaces the former 2-second
// background ticker — cleanup now happens exactly when someone
// asks for the data.
func UsageHandler(quota QuotaChecker, drainer OnDemandDrainer) gin.HandlerFunc {
	return func(c *gin.Context) {
		uc, ok := account.FromContextOK(c.Request.Context())
		if !ok || uc.UserID == "" {
			c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
			return
		}
		if drainer != nil {
			if _, err := drainer.DrainOnce(c.Request.Context()); err != nil {
				logging.FromContext(c.Request.Context()).Warn("billing.usage.drain_on_read_failed",
					slog.String("user_id", uc.UserID), slog.String("error", err.Error()))
			}
		}
		state, err := quota.CheckQuota(c.Request.Context(), uc.UserID)
		if err != nil {
			if errors.Is(err, ErrUserNotFound) {
				c.JSON(http.StatusNotFound, gin.H{"error": "user not found"})
				return
			}
			logging.FromContext(c.Request.Context()).Error("billing.usage.check_failed",
				slog.String("user_id", uc.UserID), slog.String("error", err.Error()))
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal"})
			return
		}
		c.JSON(http.StatusOK, state)
	}
}

// PlansHandler returns a Gin handler that lists the plan catalog
// (read-only). 200 with []Plan on success.
func PlansHandler(lister PlansLister) gin.HandlerFunc {
	return func(c *gin.Context) {
		plans, err := lister.ListPlans(c.Request.Context())
		if err != nil {
			logging.FromContext(c.Request.Context()).Error("billing.plans.list_failed",
				slog.String("error", err.Error()))
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal"})
			return
		}
		c.JSON(http.StatusOK, gin.H{"plans": plans})
	}
}
