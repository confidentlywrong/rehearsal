// Copyright 2026 Christian Haeussinger. Apache-2.0.

// portal_handler.go exposes the Stripe Customer Portal to
// authenticated users. The portal is a Stripe-hosted page where the
// user can update their card, switch plans, view invoices, or
// cancel. We just open a one-shot URL and redirect the browser to
// it; Stripe handles everything else.
//
// Route: POST /api/v1/billing/portal {return_url}.
//
// 409 if the user has no stripe_customer_id yet (they have never
// completed a Checkout, so there is no portal to show); 503 if the
// portal provider isn't configured.

package billing

import (
	"errors"
	"log/slog"
	"net/http"
	"strings"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/logging"
	"github.com/gin-gonic/gin"
)

// PortalHandler returns the Gin handler for the customer-portal
// endpoint. users provides the live user row (for the customer id);
// portal opens the Stripe session.
//
// allowedBaseURL is the trusted base URL the return URL must share
// an origin with (typically STRIPE_RETURN_BASE_URL). Fails closed
// with 503 portal_unavailable when empty.
func PortalHandler(users UserLookup, portal PortalCreator, allowedBaseURL string) gin.HandlerFunc {
	trimmedBase := strings.TrimSpace(allowedBaseURL)
	return func(c *gin.Context) {
		uc, ok := account.FromContextOK(c.Request.Context())
		if !ok || uc.UserID == "" {
			c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
			return
		}
		if users == nil || portal == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error": "billing portal not configured", "code": "portal_unavailable",
			})
			return
		}
		if c.Request.Body != nil {
			c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 1<<20)
		}
		var req struct {
			ReturnURL string `json:"return_url"`
		}
		if err := c.ShouldBindJSON(&req); err != nil {
			var maxBytesErr *http.MaxBytesError
			if errors.As(err, &maxBytesErr) {
				c.JSON(http.StatusRequestEntityTooLarge, gin.H{"error": "request body exceeds 1 MiB limit"})
				return
			}
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid body: " + err.Error()})
			return
		}
		if trimmedBase == "" {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error": "billing portal not configured", "code": "portal_unavailable",
			})
			return
		}
		req.ReturnURL = strings.TrimSpace(req.ReturnURL)
		if req.ReturnURL == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "return_url required"})
			return
		}
		// Open-redirect guard (MED-2 in the 2026-06-25 audit). The
		// return_url must share an origin with allowedBaseURL.
		if err := ValidateRedirectURL(req.ReturnURL, trimmedBase); err != nil {
			logging.FromContext(c.Request.Context()).Warn("billing.portal.bad_return_url",
				slog.String("user_id", uc.UserID),
				slog.String("url", req.ReturnURL),
				slog.String("error", err.Error()))
			c.JSON(http.StatusBadRequest, gin.H{"error": "return_url not allowed"})
			return
		}
		user, err := users.GetUser(c.Request.Context(), uc.UserID)
		if err != nil {
			if errors.Is(err, account.ErrNotFound) {
				c.JSON(http.StatusNotFound, gin.H{"error": "user not found"})
				return
			}
			logging.FromContext(c.Request.Context()).Error("billing.portal.user_lookup_failed",
				slog.String("user_id", uc.UserID), slog.String("error", err.Error()))
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal"})
			return
		}
		if user.StripeCustomerID == "" {
			c.JSON(http.StatusConflict, gin.H{
				"error": "no Stripe customer on file; complete a checkout first",
				"code":  "no_stripe_customer",
			})
			return
		}
		url, err := portal.Create(c.Request.Context(), user.StripeCustomerID, req.ReturnURL)
		if err != nil {
			if errors.Is(err, ErrPortalUnavailable) {
				c.JSON(http.StatusServiceUnavailable, gin.H{
					"error": "billing portal not configured", "code": "portal_unavailable",
				})
				return
			}
			logging.FromContext(c.Request.Context()).Error("billing.portal.create_failed",
				slog.String("user_id", uc.UserID), slog.String("error", err.Error()))
			c.JSON(http.StatusBadGateway, gin.H{"error": "portal create failed"})
			return
		}
		c.JSON(http.StatusOK, gin.H{"url": url})
	}
}
