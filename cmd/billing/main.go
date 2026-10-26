// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

// Command billing is the webhook-receiver micro-service for the
// per-user model: every Stripe event resolves to exactly one user
// row in the users table (either via metadata.user_id on a
// Checkout, or via stripe_customer_id on a subscription/invoice
// event).
//
// It runs OPTIONALLY: only when BILLING_PROVIDER=stripe AND a
// STRIPE_WEBHOOK_SECRET is configured does the service do any real
// work. The default `BILLING_PROVIDER=stub` causes /webhook to
// reject every request — this is intentional so a misconfigured
// local dev box can't be tricked into promoting users by POSTing
// a hand-crafted payload.
//
// The service deliberately does NOT pull in the official Stripe
// SDK. HMAC-SHA256 signature verification and the small JSON
// subset we care about live in internal/billing/stripe.go using
// only stdlib crypto + encoding.
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
	"github.com/GoogleCloudPlatform/race-condition/internal/config"
	"github.com/GoogleCloudPlatform/race-condition/internal/logging"
	"github.com/GoogleCloudPlatform/race-condition/internal/middleware"
	"github.com/GoogleCloudPlatform/race-condition/internal/tracing"
	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// planSetter is the subset of *account.Store the checkout +
// subscription webhook handlers need.
type planSetter interface {
	SetPlan(ctx context.Context, userID string, plan account.PlanTier) error
}

// userLookup is the subset of *account.Store the customer-id-keyed
// events (subscription/invoice) need to resolve the local user
// row from a Stripe customer id. GetUser is also required so
// applyPlan can short-circuit when the target user has been
// granted the complimentary (`comp`) tier — see applyPlan for
// the rationale.
type userLookup interface {
	GetUser(ctx context.Context, id string) (account.User, error)
	GetUserByStripeCustomerID(ctx context.Context, customerID string) (account.User, error)
	MarkPaymentFailed(ctx context.Context, userID string, ts time.Time) error
	ClearPaymentFailed(ctx context.Context, userID string) error
}

// auditRecorder is the subset of *account.Store the webhook
// handlers use for audit logging. Optional — passing nil disables
// audit emission so a deploy without the audit table can still
// run.
type auditRecorder interface {
	RecordAudit(ctx context.Context, e account.AuditEntry) error
}

// dbPingerForHealthz is wired by main() to the pgx Ping so /healthz
// flips to 503 when the DB is unreachable.
var dbPingerForHealthz func(ctx context.Context) error

func main() {
	logging.Setup()

	tracingShutdown, err := tracing.SetupFromEnv(context.Background(), "race-billing")
	if err != nil {
		log.Printf("⚠️ Tracing setup failed: %v (continuing without traces)", err)
	}
	defer func() { _ = tracingShutdown(context.Background()) }()

	port := config.Optional("PORT", "9122")
	if err := config.ValidatePort(port); err != nil {
		log.Fatalf("❌ Invalid PORT: %v", err)
	}

	provider, err := providerFromEnv()
	if err != nil {
		log.Fatalf("BILLING: provider init: %v", err)
	}
	log.Printf("BILLING: provider=%s", provider.Name())

	priceMap, err := billing.LoadPriceMapFromEnv(os.Getenv("STRIPE_PRICE_MAP"))
	if err != nil {
		log.Fatalf("BILLING: STRIPE_PRICE_MAP: %v", err)
	}
	log.Printf("BILLING: price map entries=%d", len(priceMap))

	store, pool, closeFn, err := openAccountStore()
	if err != nil {
		log.Printf("BILLING: account store unavailable (%v); serving /healthz only", err)
		runHealthOnly(port, provider, priceMap)
		return
	}
	defer closeFn()

	dbPingerForHealthz = pool.Ping

	// 2026-06-21 audit P2 #4: dedup is fail-closed. Falling back to a
	// per-replica in-memory LRU under multi-replica Stripe retries
	// would cause duplicate plan changes (the LRU is not shared across
	// pods). Refuse to start the webhook surface when Postgres dedup
	// is unavailable — Stripe will retry the event for up to 3 days,
	// which gives an operator time to repair the DB.
	pgDedup, pgErr := billing.NewPostgresEventDedup(pool)
	if pgErr != nil {
		log.Fatalf("BILLING: postgres dedup required; refusing to start with in-memory fallback: %v", pgErr)
	}
	log.Printf("BILLING: dedup=postgres (table=processed_webhook_events)")
	var dedup billing.EventDedup = pgDedup

	gin.SetMode(gin.ReleaseMode)
	r := newRouter(provider, priceMap, store, store, dedup, time.Now, store)

	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           r,
		ReadTimeout:       15 * time.Second,
		ReadHeaderTimeout: 5 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	go func() {
		log.Printf("BILLING: listening on :%s", port)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("BILLING: serve: %v", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Println("BILLING: shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("BILLING: forced shutdown: %v", err)
	}
}

func providerFromEnv() (billing.Provider, error) {
	switch os.Getenv("BILLING_PROVIDER") {
	case "", "stub":
		return billing.NewStubProvider(), nil
	case "stripe":
		return billing.NewStripeProvider(os.Getenv("STRIPE_WEBHOOK_SECRET"))
	default:
		return nil, fmt.Errorf("unsupported BILLING_PROVIDER=%q (want stub|stripe)", os.Getenv("BILLING_PROVIDER"))
	}
}

func openAccountStore() (*account.Store, *pgxpool.Pool, func(), error) {
	connStr := buildDBConnStr()
	if connStr == "" {
		return nil, nil, nil, errors.New("DATABASE_URL or ALLOYDB_{HOST,PORT,USER,DATABASE} required")
	}

	// 2026-06-21 audit P1 #1: *pgx.Conn is NOT safe for concurrent
	// goroutines. Cloud Run serves many requests in parallel, so the
	// runtime store must be backed by *pgxpool.Pool.
	//
	// Migrations, however, depend on a session-level
	// pg_advisory_lock(0x55534552) in internal/account/migrate.go.
	// Session locks are tied to a single connection — if the lock,
	// body, and unlock were issued through a pool they could land on
	// three different physical connections and the lock would not
	// gate the body. We therefore open a temporary single connection
	// just to apply the migration, close it, and only then open the
	// runtime pool.
	bctx, bcancel := context.WithTimeout(context.Background(), 5*time.Second)
	migConn, err := pgx.Connect(bctx, connStr)
	bcancel()
	if err != nil {
		return nil, nil, nil, fmt.Errorf("connect (migration): %w", err)
	}
	mctx, mcancel := context.WithTimeout(context.Background(), 30*time.Second)
	mErr := account.ApplyMigration(mctx, migConn)
	mcancel()
	closeCtx, closeCancel := context.WithTimeout(context.Background(), 2*time.Second)
	_ = migConn.Close(closeCtx)
	closeCancel()
	if mErr != nil {
		return nil, nil, nil, fmt.Errorf("account migration: %w", mErr)
	}

	pctx, pcancel := context.WithTimeout(context.Background(), 5*time.Second)
	pool, perr := pgxpool.New(pctx, connStr)
	pcancel()
	if perr != nil {
		return nil, nil, nil, fmt.Errorf("pool: %w", perr)
	}
	closeFn := func() {
		pool.Close()
	}
	return account.NewStore(pool), pool, closeFn, nil
}

func buildDBConnStr() string {
	if v := os.Getenv("DATABASE_URL"); v != "" {
		return v
	}
	host := os.Getenv("ALLOYDB_HOST")
	port := os.Getenv("ALLOYDB_PORT")
	user := os.Getenv("ALLOYDB_USER")
	pass := os.Getenv("ALLOYDB_PASSWORD")
	db := os.Getenv("ALLOYDB_DATABASE")
	if host == "" || port == "" || user == "" || db == "" {
		return ""
	}
	u := &url.URL{
		Scheme: "postgresql",
		User:   url.UserPassword(user, pass),
		Host:   fmt.Sprintf("%s:%s", host, port),
		Path:   "/" + db,
	}
	return u.String()
}

// runHealthOnly serves just /healthz so the supervisor sees a live
// process even without a DB. Used as a degraded fallback.
func runHealthOnly(port string, provider billing.Provider, priceMap billing.PriceTierMap) {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(middleware.RequestLogger())
	r.Use(middleware.RequestTracing("race-billing"))
	r.Use(middleware.SecurityHeaders(middleware.SecurityHeadersFromEnv()))
	r.GET("/healthz", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{
			"status":        "ok",
			"mode":          "degraded",
			"provider":      provider.Name(),
			"mapped_prices": len(priceMap),
		})
	})
	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           r,
		ReadTimeout:       15 * time.Second,
		ReadHeaderTimeout: 5 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	serverErr := make(chan error, 1)
	go func() { serverErr <- srv.ListenAndServe() }()
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	select {
	case err := <-serverErr:
		log.Fatalf("BILLING: HTTP server failed: %v", err)
	case <-quit:
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
}

// newRouter wires the webhook + healthz endpoints.
func newRouter(
	provider billing.Provider,
	priceMap billing.PriceTierMap,
	store planSetter,
	users userLookup,
	dedup billing.EventDedup,
	clock func() time.Time,
	auditor auditRecorder,
) *gin.Engine {
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(middleware.RequestLogger())
	r.Use(middleware.RequestTracing("race-billing"))
	r.Use(middleware.SecurityHeaders(middleware.SecurityHeadersFromEnv()))

	r.GET("/healthz", func(c *gin.Context) {
		if dbPingerForHealthz != nil {
			ctx, cancel := context.WithTimeout(c.Request.Context(), 1*time.Second)
			defer cancel()
			if err := dbPingerForHealthz(ctx); err != nil {
				c.JSON(http.StatusServiceUnavailable, gin.H{
					"status":        "unhealthy",
					"reason":        "postgres unreachable",
					"provider":      provider.Name(),
					"mapped_prices": len(priceMap),
				})
				return
			}
		}
		c.JSON(http.StatusOK, gin.H{
			"status":        "ok",
			"provider":      provider.Name(),
			"mapped_prices": len(priceMap),
		})
	})

	r.POST("/webhook", webhookHandler(provider, priceMap, store, users, dedup, clock, auditor))
	return r
}

func webhookHandler(
	provider billing.Provider,
	priceMap billing.PriceTierMap,
	store planSetter,
	users userLookup,
	dedup billing.EventDedup,
	clock func() time.Time,
	auditor auditRecorder,
) gin.HandlerFunc {
	return func(c *gin.Context) {
		// Cap Stripe webhook payload size. Real Stripe events are well under
		// 100 KB; a 1 MB cap is generous while still preventing an unbounded
		// io.ReadAll() from holding multi-GB requests in memory before the
		// JSON parser rejects them.
		const maxWebhookBodyBytes = 1 << 20 // 1 MiB
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxWebhookBodyBytes)
		defer c.Request.Body.Close()
		body, err := io.ReadAll(c.Request.Body)
		if err != nil {
			log.Printf("read body: %v", err)
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body"})
			return
		}

		sigHeader := c.GetHeader("Stripe-Signature")
		ev, err := provider.VerifyAndParse(body, sigHeader, clock())
		if err != nil {
			if errors.Is(err, billing.ErrSignatureInvalid) {
				c.JSON(http.StatusBadRequest, gin.H{"error": "invalid signature"})
				return
			}
			log.Printf("ShouldBindJSON: %v", err)
			c.JSON(http.StatusUnprocessableEntity, gin.H{"error": "invalid request body"})
			return
		}

		if ev.ID == "" {
			c.JSON(http.StatusUnprocessableEntity, gin.H{
				"error": "event missing id (cannot dedup)",
			})
			return
		}
		if dedup.SeenOrMark(ev.ID) {
			c.JSON(http.StatusOK, gin.H{"event_id": ev.ID, "status": "deduped"})
			return
		}
		defer func() {
			if c.Writer.Status() >= 500 {
				dedup.Forget(ev.ID)
			}
		}()

		ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
		defer cancel()

		switch ev.Type {
		case billing.EventCheckoutCompleted:
			handleCheckoutCompleted(c, ctx, ev, priceMap, store, users, auditor)
		case billing.EventSubscriptionUpdated:
			handleSubscriptionUpdated(c, ctx, ev, priceMap, store, users, auditor)
		case billing.EventSubscriptionDeleted:
			handleSubscriptionDeleted(c, ctx, ev, store, users, auditor)
		case billing.EventInvoicePaymentFailed:
			handleInvoicePaymentFailed(c, ctx, ev, users, clock, auditor)
		case billing.EventInvoicePaymentSucceeded:
			handleInvoicePaymentSucceeded(c, ctx, ev, users, auditor)
		default:
			log.Printf("BILLING: ignored event type=%s id=%s", ev.Type, ev.ID)
			c.JSON(http.StatusOK, gin.H{"event_id": ev.ID, "status": "ignored", "type": string(ev.Type)})
		}
	}
}

func handleCheckoutCompleted(
	c *gin.Context,
	ctx context.Context,
	ev *billing.WebhookEvent,
	priceMap billing.PriceTierMap,
	store planSetter,
	users userLookup,
	auditor auditRecorder,
) {
	cc := ev.CheckoutCompleted
	if cc == nil {
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"event_id": ev.ID,
			"error":    "checkout.session.completed missing payload",
		})
		return
	}
	if cc.UserID == "" {
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"event_id": ev.ID,
			"error":    "checkout session missing metadata.user_id",
		})
		return
	}
	if cc.PriceID == "" {
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"event_id": ev.ID,
			"error":    "checkout session has no line item price or metadata.price_id",
		})
		return
	}
	tier, ok := priceMap.Lookup(cc.PriceID)
	if !ok {
		logging.FromContext(ctx).Warn("billing.checkout.unmapped_price",
			slog.String("event_id", ev.ID),
			slog.String("price_id", cc.PriceID),
			slog.String("user_id", cc.UserID),
		)
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"event_id": ev.ID,
			"error":    "price_id not in STRIPE_PRICE_MAP",
			"price_id": cc.PriceID,
		})
		return
	}
	// MED-3 (2026-06-25 audit): cross-check metadata.user_id
	// against the customer_id Stripe reports. Without this check,
	// anyone who can post to /webhook (signature-bypass aside) or
	// who can craft a Stripe Checkout with another user's metadata
	// could upgrade the wrong account.
	//
	// The signature check is already a strong gate, but defense in
	// depth: if the customer_id resolves to a different user, we
	// 422 and audit-log instead of silently applying. When the
	// customer_id is absent (e.g. test fixtures predating the
	// field) or unknown (legitimate first-time customer whose row
	// hasn't been linked yet), we skip the check and proceed —
	// applyPlan is still gated by UserID lookup inside store.
	if users != nil && cc.Customer != "" {
		resolved, lerr := users.GetUserByStripeCustomerID(ctx, cc.Customer)
		switch {
		case lerr == nil && resolved.ID != "" && resolved.ID != cc.UserID:
			logging.FromContext(ctx).Warn("billing.checkout.user_id_customer_mismatch",
				slog.String("event_id", ev.ID),
				slog.String("metadata_user_id", cc.UserID),
				slog.String("customer_id", cc.Customer),
				slog.String("resolved_user_id", resolved.ID),
			)
			recordAudit(ctx, auditor, account.AuditEntry{
				UserID: resolved.ID,
				Action: account.ActionUserPlanChanged,
				Target: "plan:rejected",
				Metadata: map[string]any{
					"reason":           "metadata_user_id_customer_mismatch",
					"event_id":         ev.ID,
					"metadata_user_id": cc.UserID,
					"customer_id":      cc.Customer,
				},
			})
			c.JSON(http.StatusUnprocessableEntity, gin.H{
				"event_id": ev.ID,
				"error":    "metadata.user_id does not match customer_id",
			})
			return
		case lerr != nil && !errors.Is(lerr, account.ErrNotFound):
			logging.FromContext(ctx).Warn("billing.checkout.customer_lookup_failed",
				slog.String("event_id", ev.ID),
				slog.String("customer_id", cc.Customer),
				slog.String("error", lerr.Error()),
			)
		}
	}
	if !applyPlan(c, ctx, ev, cc.UserID, tier, store, users) {
		return
	}
	logging.FromContext(ctx).Info("billing.checkout.applied",
		slog.String("event_id", ev.ID),
		slog.String("user_id", cc.UserID),
		slog.String("plan_tier", string(tier)),
	)
	recordAudit(ctx, auditor, account.AuditEntry{
		UserID: cc.UserID,
		Action: account.ActionUserPlanChanged,
		Target: "plan:" + string(tier),
		Metadata: map[string]any{
			"to":       string(tier),
			"source":   "stripe",
			"event_id": ev.ID,
			"price_id": cc.PriceID,
		},
	})
	c.JSON(http.StatusOK, gin.H{
		"event_id":  ev.ID,
		"status":    "applied",
		"user_id":   cc.UserID,
		"plan_tier": string(tier),
	})
}

func handleSubscriptionUpdated(
	c *gin.Context,
	ctx context.Context,
	ev *billing.WebhookEvent,
	priceMap billing.PriceTierMap,
	store planSetter,
	users userLookup,
	auditor auditRecorder,
) {
	if users == nil {
		log.Printf("BILLING: subscription.updated ignored — userLookup not configured (event_id=%s)", ev.ID)
		c.JSON(http.StatusOK, gin.H{"event_id": ev.ID, "status": "ignored", "reason": "user_lookup_disabled"})
		return
	}
	sc := ev.SubscriptionChange
	if sc == nil || sc.Customer == "" {
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"event_id": ev.ID,
			"error":    "subscription event missing customer",
		})
		return
	}
	if sc.PriceID == "" {
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"event_id": ev.ID,
			"error":    "subscription.updated has no items.data[0].price.id",
		})
		return
	}

	user, err := users.GetUserByStripeCustomerID(ctx, sc.Customer)
	if err != nil {
		if errors.Is(err, account.ErrNotFound) {
			c.JSON(http.StatusUnprocessableEntity, gin.H{
				"event_id":    ev.ID,
				"error":       "no user for stripe customer",
				"customer_id": sc.Customer,
			})
			return
		}
		logging.FromContext(ctx).Error("billing.user_lookup_failed",
			slog.String("event_id", ev.ID),
			slog.String("stripe_customer", sc.Customer),
			slog.String("error", err.Error()),
		)
		log.Printf("handler error: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"event_id": ev.ID, "error": "internal server error"})
		return
	}

	tier, ok := priceMap.Lookup(sc.PriceID)
	if !ok {
		logging.FromContext(ctx).Warn("billing.subscription_updated.unmapped_price",
			slog.String("event_id", ev.ID),
			slog.String("price_id", sc.PriceID),
			slog.String("user_id", user.ID),
		)
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"event_id": ev.ID,
			"error":    "price_id not in STRIPE_PRICE_MAP",
			"price_id": sc.PriceID,
		})
		return
	}
	if !applyPlan(c, ctx, ev, user.ID, tier, store, users) {
		return
	}
	logging.FromContext(ctx).Info("billing.subscription_updated.applied",
		slog.String("event_id", ev.ID),
		slog.String("user_id", user.ID),
		slog.String("plan_tier", string(tier)),
	)
	recordAudit(ctx, auditor, account.AuditEntry{
		UserID: user.ID,
		Action: account.ActionUserPlanChanged,
		Target: "plan:" + string(tier),
		Metadata: map[string]any{
			"to":          string(tier),
			"source":      "stripe",
			"event_id":    ev.ID,
			"event_type":  string(ev.Type),
			"price_id":    sc.PriceID,
			"customer_id": sc.Customer,
		},
	})
	c.JSON(http.StatusOK, gin.H{
		"event_id":  ev.ID,
		"status":    "applied",
		"user_id":   user.ID,
		"plan_tier": string(tier),
	})
}

func handleSubscriptionDeleted(
	c *gin.Context,
	ctx context.Context,
	ev *billing.WebhookEvent,
	store planSetter,
	users userLookup,
	auditor auditRecorder,
) {
	if users == nil {
		log.Printf("BILLING: subscription.deleted ignored — userLookup not configured (event_id=%s)", ev.ID)
		c.JSON(http.StatusOK, gin.H{"event_id": ev.ID, "status": "ignored", "reason": "user_lookup_disabled"})
		return
	}
	sc := ev.SubscriptionChange
	if sc == nil || sc.Customer == "" {
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"event_id": ev.ID,
			"error":    "subscription event missing customer",
		})
		return
	}
	user, err := users.GetUserByStripeCustomerID(ctx, sc.Customer)
	if err != nil {
		if errors.Is(err, account.ErrNotFound) {
			c.JSON(http.StatusUnprocessableEntity, gin.H{
				"event_id":    ev.ID,
				"error":       "no user for stripe customer",
				"customer_id": sc.Customer,
			})
			return
		}
		logging.FromContext(ctx).Error("billing.user_lookup_failed",
			slog.String("event_id", ev.ID),
			slog.String("stripe_customer", sc.Customer),
			slog.String("error", err.Error()),
		)
		log.Printf("handler error: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"event_id": ev.ID, "error": "internal server error"})
		return
	}
	tier := account.PlanFree
	if !applyPlan(c, ctx, ev, user.ID, tier, store, users) {
		return
	}
	logging.FromContext(ctx).Info("billing.subscription_deleted.applied",
		slog.String("event_id", ev.ID),
		slog.String("user_id", user.ID),
		slog.String("plan_tier", string(tier)),
	)
	recordAudit(ctx, auditor, account.AuditEntry{
		UserID: user.ID,
		Action: account.ActionUserPlanChanged,
		Target: "plan:" + string(tier),
		Metadata: map[string]any{
			"to":          string(tier),
			"source":      "stripe",
			"event_id":    ev.ID,
			"customer_id": sc.Customer,
		},
	})
	c.JSON(http.StatusOK, gin.H{
		"event_id":  ev.ID,
		"status":    "downgraded",
		"user_id":   user.ID,
		"plan_tier": string(tier),
	})
}

func handleInvoicePaymentFailed(
	c *gin.Context,
	ctx context.Context,
	ev *billing.WebhookEvent,
	users userLookup,
	clock func() time.Time,
	auditor auditRecorder,
) {
	if users == nil {
		log.Printf("BILLING: invoice.payment_failed ignored — userLookup not configured (event_id=%s)", ev.ID)
		c.JSON(http.StatusOK, gin.H{"event_id": ev.ID, "status": "ignored", "reason": "user_lookup_disabled"})
		return
	}
	pf := ev.InvoicePaymentFailed
	if pf == nil || pf.Customer == "" {
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"event_id": ev.ID,
			"error":    "invoice event missing customer",
		})
		return
	}
	user, err := users.GetUserByStripeCustomerID(ctx, pf.Customer)
	if err != nil {
		if errors.Is(err, account.ErrNotFound) {
			c.JSON(http.StatusUnprocessableEntity, gin.H{
				"event_id":    ev.ID,
				"error":       "no user for stripe customer",
				"customer_id": pf.Customer,
			})
			return
		}
		logging.FromContext(ctx).Error("billing.user_lookup_failed",
			slog.String("event_id", ev.ID),
			slog.String("stripe_customer", pf.Customer),
			slog.String("error", err.Error()),
		)
		log.Printf("handler error: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"event_id": ev.ID, "error": "internal server error"})
		return
	}
	ts := clock()
	if err := users.MarkPaymentFailed(ctx, user.ID, ts); err != nil {
		if errors.Is(err, account.ErrNotFound) {
			c.JSON(http.StatusUnprocessableEntity, gin.H{
				"event_id": ev.ID,
				"error":    "user disappeared between lookup and update",
				"user_id":  user.ID,
			})
			return
		}
		logging.FromContext(ctx).Error("billing.mark_payment_failed_failed",
			slog.String("event_id", ev.ID),
			slog.String("user_id", user.ID),
			slog.String("error", err.Error()),
		)
		log.Printf("handler error: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"event_id": ev.ID, "error": "internal server error"})
		return
	}
	logging.FromContext(ctx).Warn("billing.invoice.payment_failed",
		slog.String("event_id", ev.ID),
		slog.String("user_id", user.ID),
		slog.String("failed_at", ts.UTC().Format(time.RFC3339)),
	)
	recordAudit(ctx, auditor, account.AuditEntry{
		UserID: user.ID,
		Action: account.ActionPaymentFailed,
		Target: user.ID,
		Metadata: map[string]any{
			"source":      "stripe",
			"event_id":    ev.ID,
			"customer_id": pf.Customer,
		},
	})
	c.JSON(http.StatusOK, gin.H{
		"event_id": ev.ID,
		"status":   "marked",
		"user_id":  user.ID,
	})
}

func handleInvoicePaymentSucceeded(
	c *gin.Context,
	ctx context.Context,
	ev *billing.WebhookEvent,
	users userLookup,
	auditor auditRecorder,
) {
	if users == nil {
		log.Printf("BILLING: invoice.payment_succeeded ignored — userLookup not configured (event_id=%s)", ev.ID)
		c.JSON(http.StatusOK, gin.H{"event_id": ev.ID, "status": "ignored", "reason": "user_lookup_disabled"})
		return
	}
	ps := ev.InvoicePaymentSucceeded
	if ps == nil || ps.Customer == "" {
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"event_id": ev.ID,
			"error":    "invoice event missing customer",
		})
		return
	}
	user, err := users.GetUserByStripeCustomerID(ctx, ps.Customer)
	if err != nil {
		if errors.Is(err, account.ErrNotFound) {
			c.JSON(http.StatusUnprocessableEntity, gin.H{
				"event_id":    ev.ID,
				"error":       "no user for stripe customer",
				"customer_id": ps.Customer,
			})
			return
		}
		logging.FromContext(ctx).Error("billing.user_lookup_failed",
			slog.String("event_id", ev.ID),
			slog.String("stripe_customer", ps.Customer),
			slog.String("error", err.Error()),
		)
		log.Printf("handler error: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"event_id": ev.ID, "error": "internal server error"})
		return
	}
	if err := users.ClearPaymentFailed(ctx, user.ID); err != nil {
		if errors.Is(err, account.ErrNotFound) {
			c.JSON(http.StatusUnprocessableEntity, gin.H{
				"event_id": ev.ID,
				"error":    "user disappeared between lookup and update",
				"user_id":  user.ID,
			})
			return
		}
		logging.FromContext(ctx).Error("billing.clear_payment_failed_failed",
			slog.String("event_id", ev.ID),
			slog.String("user_id", user.ID),
			slog.String("error", err.Error()),
		)
		log.Printf("handler error: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"event_id": ev.ID, "error": "internal server error"})
		return
	}
	logging.FromContext(ctx).Info("billing.invoice.payment_succeeded",
		slog.String("event_id", ev.ID),
		slog.String("user_id", user.ID),
	)
	recordAudit(ctx, auditor, account.AuditEntry{
		UserID: user.ID,
		Action: account.ActionPaymentRecovered,
		Target: user.ID,
		Metadata: map[string]any{
			"source":      "stripe",
			"event_id":    ev.ID,
			"customer_id": ps.Customer,
		},
	})
	c.JSON(http.StatusOK, gin.H{
		"event_id": ev.ID,
		"status":   "cleared",
		"user_id":  user.ID,
	})
}

func applyPlan(
	c *gin.Context,
	ctx context.Context,
	ev *billing.WebhookEvent,
	userID string,
	tier account.PlanTier,
	store planSetter,
	users userLookup,
) bool {
	// Comp-tier short-circuit: a user whom a superadmin has granted
	// the complimentary tier must NOT be reverted to a Stripe-driven
	// tier by any webhook (a stale checkout, a cancelled subscription,
	// etc.). Acknowledge the event with 200 OK so Stripe stops
	// retrying, log loudly, and audit so the no-op is visible. Lookup
	// failures fail OPEN — applying the Stripe-driven tier is the
	// safe default when we can't determine current state.
	if users != nil && userID != "" {
		if cur, err := users.GetUser(ctx, userID); err == nil && cur.PlanTier == account.PlanComp {
			logging.FromContext(ctx).Info("billing.apply_plan.skipped_comp_user",
				slog.String("event_id", ev.ID),
				slog.String("user_id", userID),
				slog.String("target_tier", string(tier)),
			)
			c.JSON(http.StatusOK, gin.H{
				"event_id":  ev.ID,
				"status":    "skipped",
				"reason":    "user_on_comp_tier",
				"user_id":   userID,
				"plan_tier": string(account.PlanComp),
			})
			return false
		}
	}
	if err := store.SetPlan(ctx, userID, tier); err != nil {
		switch {
		case errors.Is(err, account.ErrInvalidPlan):
			c.JSON(http.StatusUnprocessableEntity, gin.H{
				"event_id":  ev.ID,
				"error":     "tier from price map not in plans table",
				"plan_tier": string(tier),
			})
		case errors.Is(err, account.ErrNotFound):
			c.JSON(http.StatusUnprocessableEntity, gin.H{
				"event_id": ev.ID,
				"error":    "user_id not found",
				"user_id":  userID,
			})
		default:
			logging.FromContext(ctx).Error("billing.set_plan_failed",
				slog.String("event_id", ev.ID),
				slog.String("user_id", userID),
				slog.String("plan_tier", string(tier)),
				slog.String("error", err.Error()),
			)
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"event_id": ev.ID, "error": "internal server error"})
		}
		return false
	}
	return true
}

func recordAudit(ctx context.Context, auditor auditRecorder, e account.AuditEntry) {
	if auditor == nil {
		return
	}
	if err := auditor.RecordAudit(ctx, e); err != nil {
		logging.FromContext(ctx).Warn("billing.audit_failed",
			slog.String("action", e.Action),
			slog.String("user_id", e.UserID),
			slog.String("error", err.Error()),
		)
	}
}
