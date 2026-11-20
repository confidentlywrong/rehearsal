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

package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type emptyPgxRows struct{}

func (emptyPgxRows) Close()                                       {}
func (emptyPgxRows) Err() error                                   { return nil }
func (emptyPgxRows) CommandTag() pgconn.CommandTag                { return pgconn.CommandTag{} }
func (emptyPgxRows) FieldDescriptions() []pgconn.FieldDescription { return nil }
func (emptyPgxRows) Next() bool                                   { return false }
func (emptyPgxRows) Scan(...any) error                            { return nil }
func (emptyPgxRows) Values() ([]any, error)                       { return nil, nil }
func (emptyPgxRows) RawValues() [][]byte                          { return nil }
func (emptyPgxRows) Conn() *pgx.Conn                              { return nil }

type m2AdminPgxConn struct {
	fakeAdminPgxConn
	queryErr error
}

func (db *m2AdminPgxConn) Query(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
	if db.queryErr != nil {
		return nil, db.queryErr
	}
	return emptyPgxRows{}, nil
}

func TestExportUserHandler_ListAuditDBErrorReturns500(t *testing.T) {
	db := &m2AdminPgxConn{
		fakeAdminPgxConn: fakeAdminPgxConn{
			users: map[string]account.User{
				"u-exp": {ID: "u-exp", Email: "exp@example.com", PlanTier: account.PlanPro, Status: account.StatusApproved},
			},
		},
		queryErr: errors.New("db outage"),
	}
	store := account.NewStore(db)
	r := gin.New()
	r.GET("/admin/users/:id/export", exportUserHandler(store))

	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/admin/users/u-exp/export", nil))
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("expected 500 when ListAudit fails, got %d body=%s", w.Code, w.Body.String())
	}
	if db.audits != 0 {
		t.Fatalf("expected 0 ActionUserDataExported audit rows written on 500, got %d", db.audits)
	}
}

func TestExportUserHandler_EmptyAuditSerializesEmptySlice(t *testing.T) {
	db := &m2AdminPgxConn{
		fakeAdminPgxConn: fakeAdminPgxConn{
			users: map[string]account.User{
				"u-exp-ok": {ID: "u-exp-ok", Email: "exp@example.com", PlanTier: account.PlanFree, Status: account.StatusApproved},
			},
		},
	}
	store := account.NewStore(db)
	r := gin.New()
	r.GET("/admin/users/:id/export", exportUserHandler(store))

	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/admin/users/u-exp-ok/export", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `"audit":[]`) {
		t.Fatalf("expected empty audit slice to serialize as [], got %s", w.Body.String())
	}
}

type failingBreakdownUsageReader struct{}

func (failingBreakdownUsageReader) GetMonthlyUsage(_ context.Context, userID, yearMonth string) (billing.MonthlyUsage, error) {
	return billing.MonthlyUsage{
		UserID:      userID,
		YearMonth:   yearMonth,
		TotalTokens: 100,
		CostMicros:  50,
		UpdatedAt:   time.Date(2026, 11, 18, 0, 0, 0, 0, time.UTC),
	}, nil
}

func (failingBreakdownUsageReader) GetAgentBreakdown(context.Context, string, string) ([]billing.AgentBreakdown, error) {
	return nil, errors.New("ledger err")
}

func TestUsageUserHandler_GetAgentBreakdownErrorReturns500(t *testing.T) {
	r := gin.New()
	r.GET("/admin/users/:id/usage", usageUserHandler(failingBreakdownUsageReader{}, nil))

	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/admin/users/u-1/usage?year_month=2026-11", nil))
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("expected 500 on GetAgentBreakdown error, got %d body=%s", w.Code, w.Body.String())
	}
}

func TestDefaultPortMap_ContainsAllLeadershipAndCanonicalAgents(t *testing.T) {
	expected := map[string]string{
		"simulator":                      "9104",
		"planner":                        "9105",
		"planner_with_eval":              "9106",
		"simulator_with_failure":         "9107",
		"runner":                         "9108",
		"planner_with_memory":            "9109",
		"runner_autopilot":               "9110",
		"leadership_planner":             "9114",
		"leadership_planner_with_eval":   "9115",
		"leadership_planner_with_memory": "9116",
		"leadership_runner":              "9117",
		"leadership_runner_autopilot":    "9120",
		"ai_echo":                        "9123",
		"ai_echo_reactive":               "9124",
		"ai_echo_autopilot":              "9125",
	}
	for id, port := range expected {
		if got := defaultPortMap[id]; got != port {
			t.Errorf("defaultPortMap[%q] = %q, want %q", id, got, port)
		}
	}

	gwSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agent-types" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"leadership_runner": {"name":"leadership_runner","description":"LR","url":"http://127.0.0.1:9117"},
			"leadership-runner": {"name":"leadership-runner","description":"LR Hyphen","url":""},
			"ai_echo_reactive": {"name":"ai_echo_reactive","description":"Echo","url":"http://127.0.0.1:9124"},
			"cloud-agent": {"name":"cloud-agent","description":"Cloud","url":"https://cloud-agent-xyz-uc.a.run.app/a2a"}
		}`))
	}))
	defer gwSrv.Close()

	t.Setenv("LEADERSHIP_RUNNER_URL", "")
	services := loadAgentsFromGateway(gwSrv.URL)
	byID := make(map[string]ServiceInfo, len(services))
	for _, s := range services {
		byID[s.ID] = s
	}
	if byID["leadership_runner"].URL != "http://127.0.0.1:9117" {
		t.Fatalf("expected leadership_runner URL http://127.0.0.1:9117, got %q", byID["leadership_runner"].URL)
	}
	if byID["leadership-runner"].URL != "http://127.0.0.1:9117" {
		t.Fatalf("expected hyphenated leadership-runner with empty URL to resolve to http://127.0.0.1:9117, got %q", byID["leadership-runner"].URL)
	}
	if byID["ai_echo_reactive"].URL != "http://127.0.0.1:9124" {
		t.Fatalf("expected ai_echo_reactive URL http://127.0.0.1:9124, got %q", byID["ai_echo_reactive"].URL)
	}
	if byID["cloud-agent"].URL != "https://cloud-agent-xyz-uc.a.run.app" {
		t.Fatalf("expected cloud-agent Cloud Run base URL, got %q", byID["cloud-agent"].URL)
	}
}

func TestEnvironmentResetProxy_AuthAndForwarding(t *testing.T) {
	t.Setenv("GATEWAY_AUDIENCE", "")
	t.Setenv("IAP_CLIENT_ID", "")

	t.Run("local loopback does not require OIDC", func(t *testing.T) {
		if aud := resolveGatewayOIDCAudience("http://127.0.0.1:9101"); aud != "" {
			t.Fatalf("expected empty OIDC audience for local loopback, got %q", aud)
		}
		if aud := resolveGatewayOIDCAudience("https://gateway-abc-uc.a.run.app"); aud == "" {
			t.Fatalf("expected non-empty OIDC audience for Cloud Run URL")
		}
	})

	var (
		gotMethod        string
		gotPath          string
		gotBody          string
		gotAuth          string
		gotCookie        string
		gotReqID         string
		gotServerlessHdr string
		upstreamStatus   = http.StatusOK
		upstreamBody     = `{"status":"reset","results":{"sessions":{"flushed":true,"count":2}}}`
	)
	gwSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod = r.Method
		gotPath = r.URL.Path
		b, _ := io.ReadAll(r.Body)
		gotBody = string(b)
		gotAuth = r.Header.Get("Authorization")
		gotCookie = r.Header.Get("Cookie")
		gotReqID = r.Header.Get("X-Request-ID")
		gotServerlessHdr = r.Header.Get("X-Serverless-Authorization")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(upstreamStatus)
		_, _ = w.Write([]byte(upstreamBody))
	}))
	defer gwSrv.Close()

	r := setupRouter(nil, nil, nil, gwSrv.URL, nil, account.NewLocalResolver(), nil, nil)

	// Non-superadmin -> 403.
	req403 := httptest.NewRequest(http.MethodPost, "/api/admin/environment/reset", strings.NewReader(`{"targets":["sessions"]}`))
	req403.RemoteAddr = "127.0.0.1:1234"
	w403 := httptest.NewRecorder()
	r.ServeHTTP(w403, req403)
	if w403.Code != http.StatusForbidden {
		t.Fatalf("expected 403 for non-superadmin, got %d body=%s", w403.Code, w403.Body.String())
	}

	// Non-loopback -> 401.
	t.Setenv("LOCAL_RESOLVER_ALLOW_NON_LOOPBACK", "false")
	req401 := httptest.NewRequest(http.MethodPost, "/api/admin/environment/reset", strings.NewReader(`{}`))
	req401.RemoteAddr = "203.0.113.10:1234"
	req401.Header.Set(account.LocalSuperadminHeader, "1")
	w401 := httptest.NewRecorder()
	r.ServeHTTP(w401, req401)
	if w401.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 for non-loopback, got %d body=%s", w401.Code, w401.Body.String())
	}
	t.Setenv("LOCAL_RESOLVER_ALLOW_NON_LOOPBACK", "true")

	// Superadmin -> forwards and strips forged X-Serverless-Authorization.
	reqOK := httptest.NewRequest(http.MethodPost, "/api/admin/environment/reset", strings.NewReader(`{"targets":["sessions"]}`))
	reqOK.RemoteAddr = "127.0.0.1:1234"
	reqOK.Header.Set(account.LocalSuperadminHeader, "1")
	reqOK.Header.Set("Authorization", "Bearer user-tok")
	reqOK.Header.Set("Cookie", "__session=abc")
	reqOK.Header.Set("X-Request-ID", "req-123")
	reqOK.Header.Set("X-Serverless-Authorization", "Bearer forged-internal")
	wOK := httptest.NewRecorder()
	r.ServeHTTP(wOK, reqOK)
	if wOK.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", wOK.Code, wOK.Body.String())
	}
	if gotMethod != http.MethodPost || gotPath != "/api/v1/environment/reset" {
		t.Fatalf("unexpected upstream call %s %s", gotMethod, gotPath)
	}
	if gotBody != `{"targets":["sessions"]}` || gotAuth != "Bearer user-tok" || gotCookie != "__session=abc" || gotReqID != "req-123" {
		t.Fatalf("unexpected forwarded body/headers: body=%q auth=%q cookie=%q reqID=%q", gotBody, gotAuth, gotCookie, gotReqID)
	}
	if gotServerlessHdr != "" {
		t.Fatalf("forged X-Serverless-Authorization must be stripped, got %q", gotServerlessHdr)
	}

	// Upstream 400 relays cleanly.
	upstreamStatus = http.StatusBadRequest
	upstreamBody = `{"error":"unknown reset target"}`
	req400 := httptest.NewRequest(http.MethodPost, "/api/admin/environment/reset", strings.NewReader(`{"targets":["bad"]}`))
	req400.RemoteAddr = "127.0.0.1:1234"
	req400.Header.Set(account.LocalSuperadminHeader, "1")
	w400 := httptest.NewRecorder()
	r.ServeHTTP(w400, req400)
	if w400.Code != http.StatusBadRequest || !strings.Contains(w400.Body.String(), "unknown reset target") {
		t.Fatalf("expected relayed 400, got %d body=%s", w400.Code, w400.Body.String())
	}

	// Cloud Run audience with missing ADC fails closed with 502 without calling upstream.
	t.Setenv("GOOGLE_APPLICATION_CREDENTIALS", t.TempDir()+"/missing-creds.json")
	called := false
	gwFail := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))
	defer gwFail.Close()
	rFail := gin.New()
	rFail.POST("/api/admin/environment/reset", environmentResetHandler(gwFail.URL, "https://gateway-xyz.a.run.app", nil))
	reqFail := httptest.NewRequest(http.MethodPost, "/api/admin/environment/reset", strings.NewReader(`{}`))
	wFail := httptest.NewRecorder()
	rFail.ServeHTTP(wFail, reqFail)
	if wFail.Code != http.StatusBadGateway {
		t.Fatalf("expected 502 on OIDC failure, got %d body=%s", wFail.Code, wFail.Body.String())
	}
	if called {
		t.Fatalf("upstream must not be dialed when OIDC token acquisition fails")
	}

	// Unreachable Gateway -> 502.
	closedSrv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	closedURL := closedSrv.URL
	closedSrv.Close()
	rDown := gin.New()
	rDown.POST("/api/admin/environment/reset", environmentResetHandler(closedURL, "", nil))
	wDown := httptest.NewRecorder()
	rDown.ServeHTTP(wDown, httptest.NewRequest(http.MethodPost, "/api/admin/environment/reset", strings.NewReader(`{}`)))
	if wDown.Code != http.StatusBadGateway {
		t.Fatalf("expected 502 for unreachable gateway, got %d body=%s", wDown.Code, wDown.Body.String())
	}

	// Gateway timeout -> 504 gateway timeout.
	slowDone := make(chan struct{})
	slowSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-slowDone:
		}
	}))
	defer func() {
		close(slowDone)
		slowSrv.Close()
	}()
	rTimeout := gin.New()
	rTimeout.POST("/api/admin/environment/reset", environmentResetHandler(slowSrv.URL, "", &http.Client{Timeout: 25 * time.Millisecond}))
	wTimeout := httptest.NewRecorder()
	rTimeout.ServeHTTP(wTimeout, httptest.NewRequest(http.MethodPost, "/api/admin/environment/reset", strings.NewReader(`{}`)))
	if wTimeout.Code != http.StatusGatewayTimeout {
		t.Fatalf("expected 504 on gateway timeout, got %d body=%s", wTimeout.Code, wTimeout.Body.String())
	}
}

type failingSubFetcher struct{ err error }

func (f failingSubFetcher) FetchActiveSubscription(context.Context, string) (string, error) {
	return "", f.err
}

type noopPlanWriter struct{}

func (noopPlanWriter) SetPlan(context.Context, string, account.PlanTier) error { return nil }

func TestReconcileBillingHandler_InternalErrorDoesNotLeakDetails(t *testing.T) {
	db := &m2AdminPgxConn{
		fakeAdminPgxConn: fakeAdminPgxConn{
			users: map[string]account.User{
				"u-rec": {
					ID:               "u-rec",
					Email:            "rec@example.com",
					StripeCustomerID: "cus_abc",
					PlanTier:         account.PlanFree,
					Status:           account.StatusApproved,
				},
			},
		},
	}
	store := account.NewStore(db)
	rec := &billing.Reconciler{
		Users:    store,
		Fetcher:  failingSubFetcher{err: errors.New("secret stripe internal error sk_live_xyz")},
		PriceMap: billing.PriceTierMap{"price_pro": account.PlanPro},
		Writer:   noopPlanWriter{},
	}

	r := gin.New()
	r.POST("/admin/billing/reconcile", reconcileBillingHandler(store, rec))

	req := httptest.NewRequest(http.MethodPost, "/admin/billing/reconcile", strings.NewReader(`{"user_id":"u-rec"}`))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusInternalServerError {
		t.Fatalf("expected 500, got %d body=%s", w.Code, w.Body.String())
	}
	var got map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode json: %v", err)
	}
	if got["error"] != "internal server error" {
		t.Fatalf("expected generic error, got %q", got["error"])
	}
	if strings.Contains(w.Body.String(), "sk_live_xyz") {
		t.Fatalf("response leaked internal error details: %s", w.Body.String())
	}
}

func TestAdminNoRouteAndBodyLimitsAndTrustedProxies(t *testing.T) {
	t.Setenv("TRUSTED_PROXIES", "")
	r := setupRouter(nil, nil, nil, "http://127.0.0.1:9101", nil, account.NewLocalResolver(), nil, nil)

	// Unmatched /api/* and /admin/* GETs return JSON 404 (never index.html).
	for _, path := range []string{"/api/nonexistent", "/admin/nonexistent"} {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		req.RemoteAddr = "127.0.0.1:1234"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusNotFound || !strings.Contains(w.Body.String(), "not found") {
			t.Fatalf("GET %s: expected JSON 404, got %d body=%s", path, w.Code, w.Body.String())
		}
	}

	// Oversized >1 MiB body on setUserPlanHandler returns 413.
	store := account.NewStore(&m2AdminPgxConn{})
	rPlan := gin.New()
	rPlan.PUT("/admin/users/:id/plan", setUserPlanHandler(store))
	huge := `{"plan_tier":"` + strings.Repeat("a", (1<<20)+128) + `"}`
	reqPlan := httptest.NewRequest(http.MethodPut, "/admin/users/u1/plan", strings.NewReader(huge))
	reqPlan.Header.Set("Content-Type", "application/json")
	wPlan := httptest.NewRecorder()
	rPlan.ServeHTTP(wPlan, reqPlan)
	if wPlan.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("expected 413 on oversized plan update, got %d body=%s", wPlan.Code, wPlan.Body.String())
	}

	// Trailing garbage JSON rejected with 400 on setUserPlanHandler and reconcileBillingHandler.
	reqPlanGarbage := httptest.NewRequest(http.MethodPut, "/admin/users/u1/plan", strings.NewReader(`{"plan_tier":"pro"}garbage`))
	reqPlanGarbage.Header.Set("Content-Type", "application/json")
	wPlanGarbage := httptest.NewRecorder()
	rPlan.ServeHTTP(wPlanGarbage, reqPlanGarbage)
	if wPlanGarbage.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 on trailing garbage plan update, got %d body=%s", wPlanGarbage.Code, wPlanGarbage.Body.String())
	}

	rec := &billing.Reconciler{
		Users:    store,
		Fetcher:  failingSubFetcher{err: errors.New("should not run")},
		PriceMap: billing.PriceTierMap{"price_pro": account.PlanPro},
		Writer:   noopPlanWriter{},
	}
	rRec := gin.New()
	rRec.POST("/admin/billing/reconcile", reconcileBillingHandler(store, rec))
	reqRecGarbage := httptest.NewRequest(http.MethodPost, "/admin/billing/reconcile", strings.NewReader(`{"user_id":"u-rec"}garbage`))
	reqRecGarbage.Header.Set("Content-Type", "application/json")
	wRecGarbage := httptest.NewRecorder()
	rRec.ServeHTTP(wRecGarbage, reqRecGarbage)
	if wRecGarbage.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 on trailing garbage reconcile, got %d body=%s", wRecGarbage.Code, wRecGarbage.Body.String())
	}

	// Trusted proxies disabled by default.
	var gotIP string
	r.GET("/ip-check", func(c *gin.Context) {
		gotIP = c.ClientIP()
		c.Status(http.StatusOK)
	})
	reqIP := httptest.NewRequest(http.MethodGet, "/ip-check", nil)
	reqIP.RemoteAddr = "127.0.0.1:9999"
	reqIP.Header.Set("X-Forwarded-For", "198.51.100.44")
	wIP := httptest.NewRecorder()
	r.ServeHTTP(wIP, reqIP)
	if gotIP != "127.0.0.1" {
		t.Fatalf("expected spoofed X-Forwarded-For ignored, got %q", gotIP)
	}
}
