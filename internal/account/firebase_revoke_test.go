// Copyright 2026 Christian Haeussinger. Apache-2.0.
//
// Tests for the iat-vs-tokens_invalid_after revoke check
// (audit P3 #17 + the P3 #7 clock-skew grace, 2026-06-21).
//
// claimsToUser is exercised directly with a stub UserLookup so the
// test does not need a Firebase JWKS endpoint. We do not test the
// signature-verification path here — that's covered by the broader
// firebase resolver tests.

package account

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// stubLookup returns the given user (or ErrNotFound for an empty
// User.ID) for every UID. claimsToUser only uses the result, not the
// lookup key, so this is sufficient.
func stubLookup(u User) UserLookup {
	return func(ctx context.Context, uid string) (User, error) {
		if u.ID == "" {
			return User{}, ErrNotFound
		}
		return u, nil
	}
}

// activeUser builds a minimal live (non-soft-deleted) user with the
// given revoke watermark. nil revoke means "never revoked".
func activeUser(revoke *time.Time) User {
	return User{
		ID:                 "uid-1",
		Email:              "a@example.com",
		TokensInvalidAfter: revoke,
		// IsActive() requires deleted_at to be nil — User zero
		// value satisfies that.
	}
}

func mintClaims(iat time.Time) jwt.MapClaims {
	// Firebase tokens carry iat as a NumericDate (seconds since
	// epoch). jwt.MapClaims surfaces numeric values as float64
	// when round-tripped through JSON; tokenIssuedAt handles
	// float64, int64, and json.Number.
	return jwt.MapClaims{
		"sub":   "uid-1",
		"email": "a@example.com",
		"iat":   float64(iat.Unix()),
	}
}

func TestClaimsToUserRevokeCheck(t *testing.T) {
	// Anchor the test in the past so jitter from the test clock
	// can't accidentally make any iat "future relative to now".
	revoke := time.Date(2026, 6, 21, 12, 0, 0, 0, time.UTC)

	tests := []struct {
		name    string
		iat     time.Time
		want    error // nil = accept
		wantUID string
	}{
		{
			name:    "iat well after revoke is accepted",
			iat:     revoke.Add(1 * time.Hour),
			want:    nil,
			wantUID: "uid-1",
		},
		{
			name:    "iat equal to revoke is accepted (same-second relog)",
			iat:     revoke,
			want:    nil,
			wantUID: "uid-1",
		},
		{
			name:    "iat 1s before revoke accepted via clock-skew grace (P3 #7)",
			iat:     revoke.Add(-1 * time.Second),
			want:    nil,
			wantUID: "uid-1",
		},
		{
			name:    "iat exactly at -tokenRevokeSkew accepted (boundary)",
			iat:     revoke.Add(-tokenRevokeSkew),
			want:    nil,
			wantUID: "uid-1",
		},
		{
			name: "iat one second past the grace window is rejected",
			iat:  revoke.Add(-tokenRevokeSkew).Add(-1 * time.Second),
			want: ErrTokenRevoked,
		},
		{
			name: "iat well before revoke is rejected",
			iat:  revoke.Add(-1 * time.Hour),
			want: ErrTokenRevoked,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			f := &FirebaseResolver{
				cfg: FirebaseConfig{
					ProjectID: "test-project",
					Lookup:    stubLookup(activeUser(&revoke)),
				},
			}
			uc, err := f.claimsToUser(context.Background(), mintClaims(tc.iat))
			if tc.want == nil {
				if err != nil {
					t.Fatalf("want accept, got error %v", err)
				}
				if uc.UserID != tc.wantUID {
					t.Fatalf("want UserID=%q, got %q", tc.wantUID, uc.UserID)
				}
				return
			}
			if !errors.Is(err, tc.want) {
				t.Fatalf("want error %v, got %v", tc.want, err)
			}
		})
	}
}

func TestClaimsToUserMissingIatRejectedWhenRevoked(t *testing.T) {
	// A Firebase token always carries iat. If it's missing on a
	// user with a revoke watermark set, we cannot prove the token
	// post-dates the revoke and must reject. This is part of the
	// P3 #17 behavior preserved across the P3 #7 grace change.
	revoke := time.Now().UTC().Add(-1 * time.Hour)
	f := &FirebaseResolver{
		cfg: FirebaseConfig{
			ProjectID: "test-project",
			Lookup:    stubLookup(activeUser(&revoke)),
		},
	}
	claims := jwt.MapClaims{
		"sub":   "uid-1",
		"email": "a@example.com",
		// iat deliberately omitted.
	}
	if _, err := f.claimsToUser(context.Background(), claims); !errors.Is(err, ErrTokenRevoked) {
		t.Fatalf("want ErrTokenRevoked, got %v", err)
	}
}

func TestClaimsToUserNoRevokeWatermarkBypassesIatCheck(t *testing.T) {
	// Users who have never logged out have TokensInvalidAfter ==
	// nil. The iat check must be skipped entirely for them; a
	// missing iat is acceptable in that case.
	f := &FirebaseResolver{
		cfg: FirebaseConfig{
			ProjectID: "test-project",
			Lookup:    stubLookup(activeUser(nil)),
		},
	}
	claims := jwt.MapClaims{
		"sub":   "uid-1",
		"email": "a@example.com",
		// iat deliberately omitted.
	}
	uc, err := f.claimsToUser(context.Background(), claims)
	if err != nil {
		t.Fatalf("want accept, got %v", err)
	}
	if uc.UserID != "uid-1" {
		t.Fatalf("want UserID=uid-1, got %q", uc.UserID)
	}
}
