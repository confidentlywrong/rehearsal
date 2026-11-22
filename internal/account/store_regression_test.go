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
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"math"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

type regFakeRow struct {
	scanFn func(dest ...any) error
}

func (r regFakeRow) Scan(dest ...any) error { return r.scanFn(dest...) }

type regFakeConn struct {
	execFn     func(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	queryFn    func(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	queryRowFn func(ctx context.Context, sql string, args ...any) pgx.Row
}

func (f *regFakeConn) Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	if f.execFn != nil {
		return f.execFn(ctx, sql, args...)
	}
	return pgconn.CommandTag{}, nil
}

func (f *regFakeConn) Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error) {
	if f.queryFn != nil {
		return f.queryFn(ctx, sql, args...)
	}
	return nil, errors.New("unexpected Query")
}

func (f *regFakeConn) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	if f.queryRowFn != nil {
		return f.queryRowFn(ctx, sql, args...)
	}
	return regFakeRow{scanFn: func(...any) error { return pgx.ErrNoRows }}
}

type regFakeTxConn struct {
	*regFakeConn
	tx *regFakeTx
}

func (f *regFakeTxConn) Begin(context.Context) (pgx.Tx, error) {
	return f.tx, nil
}

type regFakeTx struct {
	execSQLs   []string
	committed  bool
	rolledBack bool
	execFn     func(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	queryRowFn func(ctx context.Context, sql string, args ...any) pgx.Row
}

func (tx *regFakeTx) Begin(context.Context) (pgx.Tx, error) { return tx, nil }
func (tx *regFakeTx) Commit(context.Context) error          { tx.committed = true; return nil }
func (tx *regFakeTx) Rollback(context.Context) error        { tx.rolledBack = true; return nil }
func (tx *regFakeTx) CopyFrom(context.Context, pgx.Identifier, []string, pgx.CopyFromSource) (int64, error) {
	return 0, nil
}
func (tx *regFakeTx) SendBatch(context.Context, *pgx.Batch) pgx.BatchResults { return nil }
func (tx *regFakeTx) LargeObjects() pgx.LargeObjects                        { return pgx.LargeObjects{} }
func (tx *regFakeTx) Prepare(context.Context, string, string) (*pgconn.StatementDescription, error) {
	return nil, nil
}
func (tx *regFakeTx) Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	tx.execSQLs = append(tx.execSQLs, sql)
	if tx.execFn != nil {
		return tx.execFn(ctx, sql, args...)
	}
	return pgconn.CommandTag{}, nil
}
func (tx *regFakeTx) Query(context.Context, string, ...any) (pgx.Rows, error) { return nil, nil }
func (tx *regFakeTx) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	if tx.queryRowFn != nil {
		return tx.queryRowFn(ctx, sql, args...)
	}
	return regFakeRow{scanFn: func(...any) error { return pgx.ErrNoRows }}
}
func (tx *regFakeTx) Conn() *pgx.Conn { return nil }

func TestSetSuperadmin_RowsAffectedAndGuardProbes(t *testing.T) {
	ctx := context.Background()

	// Blank email rejected.
	sEmpty := NewStore(&regFakeConn{})
	if err := sEmpty.SetSuperadmin(ctx, "   ", true); err == nil {
		t.Fatal("expected error on blank email")
	}

	// Promote unknown/deleted email -> ErrNotFound.
	sPromoteMissing := NewStore(&regFakeConn{
		execFn: func(context.Context, string, ...any) (pgconn.CommandTag, error) {
			return pgconn.NewCommandTag("UPDATE 0"), nil
		},
	})
	if err := sPromoteMissing.SetSuperadmin(ctx, "nobody@example.com", true); !errors.Is(err, ErrNotFound) {
		t.Fatalf("promote missing: got %v, want ErrNotFound", err)
	}

	// Promote live email -> nil.
	sPromoteOK := NewStore(&regFakeConn{
		execFn: func(context.Context, string, ...any) (pgconn.CommandTag, error) {
			return pgconn.NewCommandTag("UPDATE 1"), nil
		},
	})
	if err := sPromoteOK.SetSuperadmin(ctx, "live@example.com", true); err != nil {
		t.Fatalf("promote live: unexpected err %v", err)
	}

	// Demote unknown/deleted email -> ErrNotFound.
	sDemoteMissing := NewStore(&regFakeConn{
		execFn: func(context.Context, string, ...any) (pgconn.CommandTag, error) {
			return pgconn.NewCommandTag("UPDATE 0"), nil
		},
		queryRowFn: func(context.Context, string, ...any) pgx.Row {
			return regFakeRow{scanFn: func(...any) error { return pgx.ErrNoRows }}
		},
	})
	if err := sDemoteMissing.SetSuperadmin(ctx, "missing@example.com", false); !errors.Is(err, ErrNotFound) {
		t.Fatalf("demote missing: got %v, want ErrNotFound", err)
	}

	// Demote live non-admin -> nil (idempotent).
	sDemoteNonAdmin := NewStore(&regFakeConn{
		execFn: func(context.Context, string, ...any) (pgconn.CommandTag, error) {
			return pgconn.NewCommandTag("UPDATE 0"), nil
		},
		queryRowFn: func(_ context.Context, _ string, _ ...any) pgx.Row {
			return regFakeRow{scanFn: func(dest ...any) error {
				*(dest[0].(*bool)) = false
				return nil
			}}
		},
	})
	if err := sDemoteNonAdmin.SetSuperadmin(ctx, "member@example.com", false); err != nil {
		t.Fatalf("demote live non-admin: unexpected err %v", err)
	}

	// Demote last superadmin via txBeginner -> acquires pg_advisory_xact_lock and returns ErrLastSuperadmin.
	tx := &regFakeTx{
		execFn: func(_ context.Context, sql string, _ ...any) (pgconn.CommandTag, error) {
			if strings.Contains(sql, "UPDATE users") {
				return pgconn.NewCommandTag("UPDATE 0"), nil
			}
			return pgconn.NewCommandTag("SELECT 1"), nil
		},
		queryRowFn: func(_ context.Context, _ string, _ ...any) pgx.Row {
			return regFakeRow{scanFn: func(dest ...any) error {
				*(dest[0].(*bool)) = true
				return nil
			}}
		},
	}
	sTxLast := NewStore(&regFakeTxConn{regFakeConn: &regFakeConn{}, tx: tx})
	if err := sTxLast.SetSuperadmin(ctx, "last@example.com", false); !errors.Is(err, ErrLastSuperadmin) {
		t.Fatalf("demote last admin: got %v, want ErrLastSuperadmin", err)
	}
	if len(tx.execSQLs) == 0 || !strings.Contains(tx.execSQLs[0], "pg_advisory_xact_lock") {
		t.Fatalf("expected pg_advisory_xact_lock before UPDATE, got %+v", tx.execSQLs)
	}
	if !tx.rolledBack || tx.committed {
		t.Fatalf("expected tx rolledBack=true committed=false, got (%v, %v)", tx.rolledBack, tx.committed)
	}

	// Demote with another admin present -> commits cleanly.
	txOK := &regFakeTx{
		execFn: func(_ context.Context, _ string, _ ...any) (pgconn.CommandTag, error) {
			return pgconn.NewCommandTag("UPDATE 1"), nil
		},
	}
	sTxOK := NewStore(&regFakeTxConn{regFakeConn: &regFakeConn{}, tx: txOK})
	if err := sTxOK.SetSuperadmin(ctx, "one@example.com", false); err != nil {
		t.Fatalf("demote with other admin: unexpected err %v", err)
	}
	if !txOK.committed || txOK.rolledBack {
		t.Fatalf("expected tx committed=true rolledBack=false, got (%v, %v)", txOK.committed, txOK.rolledBack)
	}
}

func TestNilStore_AllMethodsReturnErrNilStore(t *testing.T) {
	ctx := context.Background()
	var nilPool *pgxpool.Pool
	var nilConn *pgx.Conn
	stores := []*Store{
		nil,
		NewStore(nil),
		NewStore(nilPool),
		NewStore(nilConn),
		{},
	}
	past := time.Now().Add(-time.Hour)
	str := "x"
	for idx, s := range stores {
		check := func(name string, err error) {
			t.Helper()
			if !errors.Is(err, ErrNilStore) {
				t.Fatalf("store[%d].%s: got %v, want ErrNilStore", idx, name, err)
			}
		}
		_, err := s.GetUser(ctx, "u1")
		check("GetUser", err)
		_, err = s.GetUserAnyState(ctx, "u1")
		check("GetUserAnyState", err)
		_, err = s.GetUserByEmail(ctx, "a@b.com")
		check("GetUserByEmail", err)
		_, err = s.GetUserByStripeCustomerID(ctx, "cus_1")
		check("GetUserByStripeCustomerID", err)
		_, err = s.GetPlan(ctx, PlanFree)
		check("GetPlan", err)
		_, err = s.ListUsers(ctx)
		check("ListUsers", err)
		_, err = s.ListAudit(ctx, "", "", 10, 0)
		check("ListAudit", err)
		_, err = s.HasAnySuperadmin(ctx)
		check("HasAnySuperadmin", err)
		_, err = s.ListSuperadminEmails(ctx)
		check("ListSuperadminEmails", err)
		_, _, err = s.CreateUser(ctx, "u1", "a@b.com", "A")
		check("CreateUser", err)
		check("SetPlan", s.SetPlan(ctx, "u1", PlanFree))
		check("SetStripeCustomerID", s.SetStripeCustomerID(ctx, "u1", "cus_1"))
		check("MarkPaymentFailed", s.MarkPaymentFailed(ctx, "u1", past))
		check("ClearPaymentFailed", s.ClearPaymentFailed(ctx, "u1"))
		check("SetSuperadmin", s.SetSuperadmin(ctx, "a@b.com", true))
		check("RevokeTokens", s.RevokeTokens(ctx, "u1"))
		check("SetUserStatus", s.SetUserStatus(ctx, "u1", StatusApproved, "adm"))
		check("SoftDeleteUser", s.SoftDeleteUser(ctx, "u1"))
		check("UpdateUserProfile", s.UpdateUserProfile(ctx, "u1", &str, &str))
		check("HardDeleteUser", s.HardDeleteUser(ctx, "u1"))
		_, err = s.HardDeleteExpiredUsers(ctx, past)
		check("HardDeleteExpiredUsers", err)
		check("RecordAudit", s.RecordAudit(ctx, AuditEntry{Action: "test"}))
	}
}

type regAuditRows struct {
	items []struct {
		id     int64
		action string
		meta   []byte
	}
	idx int
}

func (r *regAuditRows) Close()                                       {}
func (r *regAuditRows) Err() error                                   { return nil }
func (r *regAuditRows) CommandTag() pgconn.CommandTag                { return pgconn.CommandTag{} }
func (r *regAuditRows) FieldDescriptions() []pgconn.FieldDescription { return nil }
func (r *regAuditRows) Next() bool                                   { return r.idx < len(r.items) }
func (r *regAuditRows) Scan(dest ...any) error {
	it := r.items[r.idx]
	r.idx++
	*(dest[0].(*int64)) = it.id
	*(dest[1].(*string)) = "u-1"
	*(dest[2].(*string)) = "adm-1"
	*(dest[3].(*string)) = it.action
	*(dest[4].(*string)) = ""
	*(dest[5].(*[]byte)) = it.meta
	*(dest[6].(*time.Time)) = time.Unix(1700000000, 0).UTC()
	return nil
}
func (r *regAuditRows) Values() ([]any, error) { return nil, nil }
func (r *regAuditRows) RawValues() [][]byte    { return nil }
func (r *regFakeRowsConn) Conn() *pgx.Conn     { return nil }

type regFakeRowsConn struct{ *regAuditRows }

func TestAudit_JSONErrorsAndLimitClamping(t *testing.T) {
	ctx := context.Background()

	// RecordAudit rejects non-JSON metadata and blank action.
	execCalled := false
	s := NewStore(&regFakeConn{
		execFn: func(context.Context, string, ...any) (pgconn.CommandTag, error) {
			execCalled = true
			return pgconn.CommandTag{}, nil
		},
	})
	if err := s.RecordAudit(ctx, AuditEntry{Action: "   "}); err == nil || execCalled {
		t.Fatalf("expected blank Action error without DB exec, got err=%v execCalled=%v", err, execCalled)
	}
	if err := s.RecordAudit(ctx, AuditEntry{Action: "test", Metadata: map[string]any{"bad": math.NaN()}}); err == nil || execCalled {
		t.Fatalf("expected NaN Metadata error without DB exec, got err=%v execCalled=%v", err, execCalled)
	}

	// ListAudit propagates decodeJSON error on malformed JSON.
	badRows := &regAuditRows{items: []struct {
		id     int64
		action string
		meta   []byte
	}{{id: 42, action: "x", meta: []byte("{not-json")}}}
	sBad := NewStore(&regFakeConn{
		queryFn: func(context.Context, string, ...any) (pgx.Rows, error) {
			return &regFakeRowsConn{badRows}, nil
		},
	})
	if _, err := sBad.ListAudit(ctx, "", "", 10, 0); err == nil || !strings.Contains(err.Error(), "decode audit metadata") {
		t.Fatalf("expected decodeJSON error in ListAudit, got %v", err)
	}

	// ListAudit clamps >500 to 500 (not 50) and <=0 to 50, and handles valid/"null"/{} JSON.
	var gotLimits []int
	sClamp := NewStore(&regFakeConn{
		queryFn: func(_ context.Context, _ string, args ...any) (pgx.Rows, error) {
			gotLimits = append(gotLimits, args[2].(int))
			rows := &regAuditRows{items: []struct {
				id     int64
				action string
				meta   []byte
			}{
				{id: 1, action: "a1", meta: []byte(`{"k":"v"}`)},
				{id: 2, action: "a2", meta: []byte(`null`)},
				{id: 3, action: "a3", meta: []byte(`{}`)},
			}}
			return &regFakeRowsConn{rows}, nil
		},
	})
	entries, err := sClamp.ListAudit(ctx, "", "", 1000, -5)
	if err != nil {
		t.Fatalf("ListAudit valid: %v", err)
	}
	if len(entries) != 3 || entries[0].Metadata["k"] != "v" || entries[1].Metadata == nil || entries[2].Metadata == nil {
		t.Fatalf("unexpected decoded entries: %+v", entries)
	}
	_, _ = sClamp.ListAudit(ctx, "", "", 0, 0)
	_, _ = sClamp.ListAudit(ctx, "", "", -1, 0)
	if len(gotLimits) != 3 || gotLimits[0] != 500 || gotLimits[1] != 50 || gotLimits[2] != 50 {
		t.Fatalf("unexpected clamped limits: %v (want [500 50 50])", gotLimits)
	}
}

func TestCreateUser_PropagatesNonNotFoundErrors(t *testing.T) {
	ctx := context.Background()
	dbErr := errors.New("connection refused")

	// 1. Pre-check failure propagates without reaching INSERT.
	insertCalled := false
	sPre := NewStore(&regFakeConn{
		queryRowFn: func(_ context.Context, sql string, _ ...any) pgx.Row {
			if strings.Contains(sql, "INSERT INTO users") {
				insertCalled = true
			}
			return regFakeRow{scanFn: func(...any) error { return dbErr }}
		},
	})
	if _, _, err := sPre.CreateUser(ctx, "u1", "a@b.com", "A"); !errors.Is(err, dbErr) || insertCalled {
		t.Fatalf("expected pre-check error %v without INSERT, got err=%v insertCalled=%v", dbErr, err, insertCalled)
	}

	// 2. 23505 race-loser re-read failure propagates DB error instead of ErrEmailTaken.
	sRace := NewStore(&regFakeConn{
		queryRowFn: func(_ context.Context, sql string, _ ...any) pgx.Row {
			if strings.Contains(sql, "INSERT INTO users") {
				return regFakeRow{scanFn: func(...any) error {
					return &pgconn.PgError{Code: "23505", Message: "duplicate"}
				}}
			}
			if strings.Contains(sql, "WHERE id = $1 AND deleted_at IS NULL") {
				// First pre-check returns ErrNotFound, second (after INSERT) returns dbErr.
				return regFakeRow{scanFn: func(...any) error { return pgx.ErrNoRows }}
			}
			return regFakeRow{scanFn: func(...any) error { return pgx.ErrNoRows }}
		},
	})
	calls := 0
	sRace.db.(*regFakeConn).queryRowFn = func(_ context.Context, sql string, _ ...any) pgx.Row {
		if strings.Contains(sql, "INSERT INTO users") {
			calls++
			return regFakeRow{scanFn: func(...any) error {
				return &pgconn.PgError{Code: "23505", Message: "duplicate"}
			}}
		}
		if calls > 0 {
			return regFakeRow{scanFn: func(...any) error { return dbErr }}
		}
		return regFakeRow{scanFn: func(...any) error { return pgx.ErrNoRows }}
	}
	if _, _, err := sRace.CreateUser(ctx, "u1", "a@b.com", "A"); !errors.Is(err, dbErr) {
		t.Fatalf("expected 23505 re-read error %v, got %v", dbErr, err)
	}
}

func TestStore_InputGuardsAndSetUserStatusSQL(t *testing.T) {
	ctx := context.Background()
	var gotSQL string
	var gotArgs []any
	s := NewStore(&regFakeConn{
		execFn: func(_ context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
			gotSQL = sql
			gotArgs = args
			return pgconn.NewCommandTag("UPDATE 1"), nil
		},
	})

	if err := s.MarkPaymentFailed(ctx, "u1", time.Time{}); err == nil {
		t.Fatal("expected error for zero timestamp in MarkPaymentFailed")
	}
	if err := s.SetPlan(ctx, "   ", PlanFree); err == nil {
		t.Fatal("expected error for blank userID in SetPlan")
	}
	if err := s.SetUserStatus(ctx, " u1 ", StatusApproved, ""); err != nil {
		t.Fatalf("SetUserStatus err: %v", err)
	}
	if !strings.Contains(gotSQL, "NULLIF($4, '')") || !strings.Contains(gotSQL, "WHEN status = 'approved' THEN approved_at") {
		t.Fatalf("expected NULLIF and approved_at preservation in SetUserStatus SQL, got: %s", gotSQL)
	}
	if gotArgs[0] != "u1" || gotArgs[3] != "" {
		t.Fatalf("unexpected args: %+v", gotArgs)
	}
}

func TestSecurityAndNilSafety_ResolversAndKeys(t *testing.T) {
	// 1. NewFirebaseResolver refuses EmulatorHost on GCP runtime.
	t.Setenv("K_SERVICE", "race-gateway")
	if _, err := NewFirebaseResolver(FirebaseConfig{ProjectID: "p1", EmulatorHost: "127.0.0.1:9099"}); err == nil {
		t.Fatal("expected NewFirebaseResolver to refuse EmulatorHost in GCP runtime")
	}

	// 2. Default HTTPClient has non-zero Timeout.
	t.Setenv("K_SERVICE", "")
	fr, err := NewFirebaseResolver(FirebaseConfig{ProjectID: " p1 "})
	if err != nil {
		t.Fatalf("NewFirebaseResolver: %v", err)
	}
	if fr.cfg.HTTPClient == nil || fr.cfg.HTTPClient.Timeout == 0 {
		t.Fatalf("expected non-zero HTTPClient.Timeout, got %v", fr.cfg.HTTPClient)
	}
	acv, err := NewAppCheckVerifier(AppCheckConfig{ProjectNumber: " 123 "})
	if err != nil {
		t.Fatalf("NewAppCheckVerifier: %v", err)
	}
	if acv.cfg.HTTPClient == nil || acv.cfg.HTTPClient.Timeout == 0 {
		t.Fatalf("expected non-zero AppCheck HTTPClient.Timeout, got %v", acv.cfg.HTTPClient)
	}

	// 3. Nil receivers and uninitialized structs return errors (no panic).
	var nilFR *FirebaseResolver
	if nilFR.WithLookup(nil) != nil {
		t.Fatal("nil WithLookup must return nil")
	}
	if _, err := nilFR.Resolve(httptest.NewRequest(http.MethodGet, "/", nil)); err == nil {
		t.Fatal("nil FirebaseResolver.Resolve must return error")
	}
	if _, err := fr.Resolve(nil); !errors.Is(err, ErrNoIdentity) {
		t.Fatalf("FirebaseResolver.Resolve(nil) = %v, want ErrNoIdentity", err)
	}
	uninitReq := httptest.NewRequest(http.MethodGet, "/", nil)
	uninitReq.Header.Set("Authorization", "Bearer header.payload.sig")
	if _, err := (&FirebaseResolver{cfg: FirebaseConfig{ProjectID: "p1"}}).Resolve(uninitReq); err == nil {
		t.Fatal("uninitialized FirebaseResolver.Resolve must return error")
	}
	if _, err := (&AppCheckVerifier{}).Verify(context.Background(), "a.b.c"); err == nil {
		t.Fatal("uninitialized AppCheckVerifier.Verify must return error")
	}

	// 4. Emulator claim validation: exp==now, nbf future, iat future, aud array.
	fixedNow := time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC)
	emuFR, err := NewFirebaseResolver(FirebaseConfig{
		ProjectID:    "proj-emu",
		EmulatorHost: "127.0.0.1:9099",
		Now:          func() time.Time { return fixedNow },
	})
	if err != nil {
		t.Fatalf("NewFirebaseResolver emu: %v", err)
	}
	signUnsigned := func(claims jwt.MapClaims) *http.Request {
		tok, _ := jwt.NewWithClaims(jwt.SigningMethodNone, claims).SignedString(jwt.UnsafeAllowNoneSignatureType)
		r := httptest.NewRequest(http.MethodGet, "/", nil)
		r.Header.Set("Authorization", "Bearer "+tok)
		return r
	}
	// exp == now must fail.
	if _, err := emuFR.Resolve(signUnsigned(jwt.MapClaims{
		"iss": "https://securetoken.google.com/proj-emu",
		"aud": "proj-emu",
		"sub": "u1",
		"iat": fixedNow.Add(-time.Minute).Unix(),
		"exp": fixedNow.Unix(),
	})); err == nil {
		t.Fatal("expected exp==now to be rejected in emulator mode")
	}
	// nbf in future must fail.
	if _, err := emuFR.Resolve(signUnsigned(jwt.MapClaims{
		"iss": "https://securetoken.google.com/proj-emu",
		"aud": "proj-emu",
		"sub": "u1",
		"iat": fixedNow.Add(-time.Minute).Unix(),
		"nbf": fixedNow.Add(time.Minute).Unix(),
		"exp": fixedNow.Add(time.Hour).Unix(),
	})); err == nil {
		t.Fatal("expected future nbf to be rejected in emulator mode")
	}
	// iat in future must fail.
	if _, err := emuFR.Resolve(signUnsigned(jwt.MapClaims{
		"iss": "https://securetoken.google.com/proj-emu",
		"aud": "proj-emu",
		"sub": "u1",
		"iat": fixedNow.Add(time.Minute).Unix(),
		"exp": fixedNow.Add(time.Hour).Unix(),
	})); err == nil {
		t.Fatal("expected future iat to be rejected in emulator mode")
	}
	// aud as array should succeed.
	if uc, err := emuFR.Resolve(signUnsigned(jwt.MapClaims{
		"iss": "https://securetoken.google.com/proj-emu",
		"aud": []string{"other", "proj-emu"},
		"sub": "u1",
		"iat": fixedNow.Add(-time.Minute).Unix(),
		"exp": fixedNow.Add(time.Hour).Unix(),
	})); err != nil || uc.UserID != "u1" {
		t.Fatalf("expected valid emulator token with aud array to succeed, got uc=%+v err=%v", uc, err)
	}

	// 5. Empty JWKS responses do not poison existing cached keys.
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "test"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
	}
	der, _ := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	certPEM := string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}))

	returnEmpty := false
	fbJWKS := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if returnEmpty {
			_, _ = w.Write([]byte(`{}`))
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]string{"kid-1": certPEM})
	}))
	defer fbJWKS.Close()
	cur := fixedNow
	fbCache := newJWKSCache(fbJWKS.URL, fbJWKS.Client(), func() time.Time { return cur })
	if _, err := fbCache.get(context.Background(), "kid-1"); err != nil {
		t.Fatalf("warm fbCache: %v", err)
	}
	returnEmpty = true
	cur = cur.Add(2 * time.Hour)
	if err := fbCache.refresh(context.Background()); err == nil {
		t.Fatal("expected 0-key jwksCache refresh to error")
	}
	if _, ok := fbCache.keys["kid-1"]; !ok {
		t.Fatal("expected existing fbCache keys to remain intact after 0-key error")
	}

	// AppCheck JWKS rejects empty modulus, e < 3, and 0-key responses.
	acMode := "empty_n"
	acJWKS := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		n := base64.RawURLEncoding.EncodeToString(key.N.Bytes())
		switch acMode {
		case "empty_n":
			_, _ = w.Write([]byte(`{"keys":[{"kty":"RSA","kid":"k1","n":"","e":"AQAB"}]}`))
		case "small_e":
			_ = json.NewEncoder(w).Encode(map[string]any{"keys": []map[string]any{{"kty": "RSA", "kid": "k1", "n": n, "e": "Ag"}}}) // e=2
		case "zero_keys":
			_, _ = w.Write([]byte(`{"keys":[]}`))
		}
	}))
	defer acJWKS.Close()
	acCache := newJWKSJSONCache(acJWKS.URL, acJWKS.Client())
	for _, mode := range []string{"empty_n", "small_e", "zero_keys"} {
		acMode = mode
		if err := acCache.refresh(context.Background()); err == nil {
			t.Fatalf("appcheck jwks mode %s: expected error", mode)
		}
	}

	// 6. LocalResolver nil request & GCP runtime guard.
	var lr LocalResolver
	if _, err := lr.Resolve(nil); !errors.Is(err, ErrNoIdentity) {
		t.Fatalf("LocalResolver.Resolve(nil) = %v, want ErrNoIdentity", err)
	}
	loopReq := httptest.NewRequest(http.MethodGet, "/", nil)
	loopReq.RemoteAddr = "127.0.0.1:12345"
	t.Setenv("K_SERVICE", "race-gateway")
	t.Setenv("ALLOW_LOCAL_AUTH", "")
	if _, err := lr.Resolve(loopReq); err == nil {
		t.Fatal("expected LocalResolver.Resolve to refuse GCP runtime without ALLOW_LOCAL_AUTH=1")
	}
	t.Setenv("ALLOW_LOCAL_AUTH", "1")
	if _, err := lr.Resolve(loopReq); err != nil {
		t.Fatalf("expected LocalResolver.Resolve to succeed with ALLOW_LOCAL_AUTH=1, got %v", err)
	}

	// 7. keys.go trims SAAS_MODE and ACCOUNT_KEY_PREFIX.
	t.Setenv("SAAS_MODE", " on ")
	t.Setenv("ACCOUNT_KEY_PREFIX", " tenant ")
	if !SaaSModeEnabled() {
		t.Fatal("expected SAAS_MODE=' on ' to enable SaaSModeEnabled()")
	}
	if got := KeyForUser("u1", "seg"); got != "tenant:u1:seg" {
		t.Fatalf("KeyForUser = %q, want tenant:u1:seg", got)
	}
}

func TestLocalResolver_NilPointerReceiverReturnsError(t *testing.T) {
	var res Resolver = (*LocalResolver)(nil)
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.RemoteAddr = "127.0.0.1:12345"

	if _, err := res.Resolve(req); err == nil {
		t.Fatal("expected (*LocalResolver)(nil).Resolve(req) to return an error without panicking")
	}
	if _, err := (*LocalResolver)(nil).Resolve(nil); err == nil {
		t.Fatal("expected (*LocalResolver)(nil).Resolve(nil) to return an error without panicking")
	}
}

func TestFirebaseResolver_ClaimsToUserRejectsWhitespaceUID(t *testing.T) {
	t.Setenv("K_SERVICE", "")
	fixedNow := time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC)
	emuFR, err := NewFirebaseResolver(FirebaseConfig{
		ProjectID:    "proj-emu",
		EmulatorHost: "127.0.0.1:9099",
		Now:          func() time.Time { return fixedNow },
	})
	if err != nil {
		t.Fatalf("NewFirebaseResolver: %v", err)
	}

	// Direct claimsToUser checks with Lookup == nil.
	if uc, err := emuFR.claimsToUser(context.Background(), jwt.MapClaims{"sub": "   "}); err == nil || uc.HasIdentity() {
		t.Fatalf("expected error and empty UserContext for whitespace-only sub, got uc=%+v err=%v", uc, err)
	}
	if uc, err := emuFR.claimsToUser(context.Background(), jwt.MapClaims{"sub": "\t\n ", "user_id": "   "}); err == nil || uc.HasIdentity() {
		t.Fatalf("expected error and empty UserContext for whitespace-only sub+user_id, got uc=%+v err=%v", uc, err)
	}
	if uc, err := emuFR.claimsToUser(context.Background(), jwt.MapClaims{"sub": "   ", "user_id": "uid-fallback"}); err != nil || uc.UserID != "uid-fallback" {
		t.Fatalf("expected fallback to valid user_id when sub is whitespace, got uc=%+v err=%v", uc, err)
	}

	// Full Resolve path in emulator mode with Lookup == nil.
	tok, err := jwt.NewWithClaims(jwt.SigningMethodNone, jwt.MapClaims{
		"iss": "https://securetoken.google.com/proj-emu",
		"aud": "proj-emu",
		"sub": "   ",
		"iat": fixedNow.Add(-time.Minute).Unix(),
		"exp": fixedNow.Add(time.Hour).Unix(),
	}).SignedString(jwt.UnsafeAllowNoneSignatureType)
	if err != nil {
		t.Fatalf("SignedString: %v", err)
	}
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("Authorization", "Bearer "+tok)
	if uc, err := emuFR.Resolve(req); err == nil || uc.HasIdentity() {
		t.Fatalf("expected Resolve to reject whitespace-only sub in emulator mode, got uc=%+v err=%v", uc, err)
	}
}
