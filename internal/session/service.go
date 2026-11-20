// Copyright 2026 Google LLC
// Modifications copyright 2026 Christian Haeussinger
// Changed for The Rehearsal: adapted simulation runtime, agents, and infrastructure for executive leadership rehearsal.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package session

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

const defaultInMemorySessionTTL = 24 * time.Hour

func validateSessionID(sessionID string) error {
	if sessionID == "" {
		return errors.New("session: sessionID cannot be empty")
	}
	if strings.ContainsRune(sessionID, ':') {
		return fmt.Errorf("session: sessionID %q must not contain ':'", sessionID)
	}
	for _, ch := range sessionID {
		if unicode.IsSpace(ch) || unicode.IsControl(ch) {
			return fmt.Errorf("session: sessionID %q must not contain whitespace or control characters", sessionID)
		}
	}
	return nil
}

func validateAgentType(agentType string) error {
	if strings.TrimSpace(agentType) == "" {
		return errors.New("session: agentType cannot be empty")
	}
	for _, ch := range agentType {
		if unicode.IsSpace(ch) || unicode.IsControl(ch) {
			return fmt.Errorf("session: agentType %q must not contain whitespace or control characters", agentType)
		}
	}
	return nil
}

// ErrInvalidSimulationID is returned when a non-empty simulation_id fails format validation.
var ErrInvalidSimulationID = errors.New("invalid simulation_id")

var validSimulationIDRe = regexp.MustCompile(`^[a-zA-Z0-9_-]{1,128}$`)

// ValidateSimulationID validates an optional simulation_id against the canonical
// ^[a-zA-Z0-9_-]{1,128}$ format (empty is allowed). Rejects ':', Redis glob
// characters, whitespace, control characters, and oversized identifiers.
func ValidateSimulationID(simID string) error {
	if simID == "" {
		return nil
	}
	if !validSimulationIDRe.MatchString(simID) {
		return fmt.Errorf("%w: must match ^[a-zA-Z0-9_-]{1,128}$", ErrInvalidSimulationID)
	}
	return nil
}

func validateSimulationID(simID string) error {
	return ValidateSimulationID(simID)
}

// DistributedRegistry defines the interface for cross-gateway session tracking.
// All gateway instances are horizontally scaled behind a load balancer — any
// instance can handle any request. The registry maps sessions to agent types
// so the gateway can route broadcasts to only agent types with active sessions.
type DistributedRegistry interface {
	TrackSession(ctx context.Context, sessionID string, agentType string, simulationID string) error
	BatchTrackSessions(ctx context.Context, sessions []SessionTrackingEntry) error
	FindAgentType(ctx context.Context, sessionID string) (string, bool, error)
	ActiveAgentTypes(ctx context.Context) ([]string, error)
	UntrackSession(ctx context.Context, sessionID string) error
	ListSessions(ctx context.Context) ([]string, error)
	FindSimulation(ctx context.Context, sessionID string) (string, error)
	ListSimulations(ctx context.Context) ([]string, error)
	Flush(ctx context.Context) (int, error)
	Reap(ctx context.Context) (int, error)
}

type inMemoryNamespace struct {
	agentTypes    map[string]string          // sessionID -> agentType
	agentSessions map[string]map[string]bool // agentType -> set of sessionIDs
	sessionSim    map[string]string          // sessionID -> simulationID
	simSessions   map[string]map[string]bool // simulationID -> set of sessionIDs
	lastSeen      map[string]time.Time
}

func newInMemoryNamespace() *inMemoryNamespace {
	return &inMemoryNamespace{
		agentTypes:    make(map[string]string),
		agentSessions: make(map[string]map[string]bool),
		sessionSim:    make(map[string]string),
		simSessions:   make(map[string]map[string]bool),
		lastSeen:      make(map[string]time.Time),
	}
}

func (ns *inMemoryNamespace) deleteSessionLocked(sid string) {
	if agentType, ok := ns.agentTypes[sid]; ok {
		delete(ns.agentSessions[agentType], sid)
		if len(ns.agentSessions[agentType]) == 0 {
			delete(ns.agentSessions, agentType)
		}
	}
	delete(ns.agentTypes, sid)
	delete(ns.lastSeen, sid)
	if simID, ok := ns.sessionSim[sid]; ok {
		delete(ns.simSessions[simID], sid)
		if len(ns.simSessions[simID]) == 0 {
			delete(ns.simSessions, simID)
		}
		delete(ns.sessionSim, sid)
	}
}

func (ns *inMemoryNamespace) evictExpiredLocked(now time.Time, ttl time.Duration) int {
	if ttl <= 0 {
		return 0
	}
	threshold := now.Add(-ttl)
	count := 0
	for sid, last := range ns.lastSeen {
		if !last.After(threshold) {
			ns.deleteSessionLocked(sid)
			count++
		}
	}
	return count
}

func (ns *inMemoryNamespace) trackLocked(sessionID, agentType, simulationID string, now time.Time) {
	if prevAgent, ok := ns.agentTypes[sessionID]; ok && prevAgent != agentType {
		if set := ns.agentSessions[prevAgent]; set != nil {
			delete(set, sessionID)
			if len(set) == 0 {
				delete(ns.agentSessions, prevAgent)
			}
		}
	}
	ns.agentTypes[sessionID] = agentType
	if ns.agentSessions[agentType] == nil {
		ns.agentSessions[agentType] = make(map[string]bool)
	}
	ns.agentSessions[agentType][sessionID] = true

	if prevSim, ok := ns.sessionSim[sessionID]; ok && prevSim != simulationID {
		if set := ns.simSessions[prevSim]; set != nil {
			delete(set, sessionID)
			if len(set) == 0 {
				delete(ns.simSessions, prevSim)
			}
		}
		if simulationID == "" {
			delete(ns.sessionSim, sessionID)
		}
	}
	if simulationID != "" {
		ns.sessionSim[sessionID] = simulationID
		if ns.simSessions[simulationID] == nil {
			ns.simSessions[simulationID] = make(map[string]bool)
		}
		ns.simSessions[simulationID][sessionID] = true
	}
	ns.lastSeen[sessionID] = now
}

// InMemorySessionService implements DistributedRegistry using per-tenant in-memory maps.
// This is used for local development and tests while maintaining multi-tenant isolation parity
// with RedisSessionRegistry when SAAS_MODE is enabled.
type InMemorySessionService struct {
	mu         sync.Mutex
	ttl        time.Duration
	namespaces map[string]*inMemoryNamespace
}

func NewInMemorySessionService() *InMemorySessionService {
	return NewInMemorySessionServiceWithTTL(defaultInMemorySessionTTL)
}

// NewInMemorySessionServiceWithTTL creates an InMemorySessionService with a custom session TTL.
// If ttl <= 0, the default 24-hour TTL is used.
func NewInMemorySessionServiceWithTTL(ttl time.Duration) *InMemorySessionService {
	if ttl <= 0 {
		ttl = defaultInMemorySessionTTL
	}
	return &InMemorySessionService{
		ttl:        ttl,
		namespaces: make(map[string]*inMemoryNamespace),
	}
}

func normalizeCtx(ctx context.Context) (context.Context, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	if err := ctx.Err(); err != nil {
		return ctx, err
	}
	return ctx, nil
}

func (s *InMemorySessionService) namespaceForLocked(ctx context.Context, create bool) *inMemoryNamespace {
	if s.namespaces == nil {
		if !create {
			return nil
		}
		s.namespaces = make(map[string]*inMemoryNamespace)
	}
	key := account.KeyPrefix(ctx)
	ns := s.namespaces[key]
	if ns == nil && create {
		ns = newInMemoryNamespace()
		s.namespaces[key] = ns
	}
	return ns
}

func (s *InMemorySessionService) TrackSession(ctx context.Context, sessionID, agentType, simulationID string) error {
	if s == nil {
		return errors.New("session: nil service")
	}
	ctx, err := normalizeCtx(ctx)
	if err != nil {
		return err
	}
	if err := validateSessionID(sessionID); err != nil {
		return err
	}
	if err := validateAgentType(agentType); err != nil {
		return err
	}
	if err := validateSimulationID(simulationID); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ns := s.namespaceForLocked(ctx, true)
	ns.trackLocked(sessionID, agentType, simulationID, time.Now())
	return nil
}

func (s *InMemorySessionService) BatchTrackSessions(ctx context.Context, sessions []SessionTrackingEntry) error {
	if s == nil {
		return errors.New("session: nil service")
	}
	ctx, err := normalizeCtx(ctx)
	if err != nil {
		return err
	}
	for _, sess := range sessions {
		if err := validateSessionID(sess.SessionID); err != nil {
			return err
		}
		if err := validateAgentType(sess.AgentType); err != nil {
			return err
		}
		if err := validateSimulationID(sess.SimulationID); err != nil {
			return err
		}
	}
	if len(sessions) == 0 {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ns := s.namespaceForLocked(ctx, true)
	now := time.Now()
	for _, sess := range sessions {
		ns.trackLocked(sess.SessionID, sess.AgentType, sess.SimulationID, now)
	}
	return nil
}

func (s *InMemorySessionService) FindAgentType(ctx context.Context, sessionID string) (string, bool, error) {
	if s == nil {
		return "", false, errors.New("session: nil service")
	}
	ctx, err := normalizeCtx(ctx)
	if err != nil {
		return "", false, err
	}
	if err := validateSessionID(sessionID); err != nil {
		return "", false, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ns := s.namespaceForLocked(ctx, false)
	if ns == nil {
		return "", false, nil
	}
	now := time.Now()
	ns.evictExpiredLocked(now, s.ttl)
	at, ok := ns.agentTypes[sessionID]
	if ok {
		ns.lastSeen[sessionID] = now
	}
	return at, ok, nil
}

func (s *InMemorySessionService) ActiveAgentTypes(ctx context.Context) ([]string, error) {
	if s == nil {
		return nil, errors.New("session: nil service")
	}
	ctx, err := normalizeCtx(ctx)
	if err != nil {
		return nil, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ns := s.namespaceForLocked(ctx, false)
	if ns == nil {
		return nil, nil
	}
	ns.evictExpiredLocked(time.Now(), s.ttl)
	var types []string
	for agentType, sessions := range ns.agentSessions {
		if len(sessions) > 0 {
			types = append(types, agentType)
		}
	}
	return types, nil
}

func (s *InMemorySessionService) UntrackSession(ctx context.Context, sessionID string) error {
	if s == nil {
		return errors.New("session: nil service")
	}
	ctx, err := normalizeCtx(ctx)
	if err != nil {
		return err
	}
	if err := validateSessionID(sessionID); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ns := s.namespaceForLocked(ctx, false)
	if ns == nil {
		return nil
	}
	ns.deleteSessionLocked(sessionID)
	return nil
}

func (s *InMemorySessionService) ListSessions(ctx context.Context) ([]string, error) {
	if s == nil {
		return nil, errors.New("session: nil service")
	}
	ctx, err := normalizeCtx(ctx)
	if err != nil {
		return nil, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ns := s.namespaceForLocked(ctx, false)
	if ns == nil {
		return nil, nil
	}
	ns.evictExpiredLocked(time.Now(), s.ttl)
	sessions := make([]string, 0, len(ns.agentTypes))
	for sid := range ns.agentTypes {
		sessions = append(sessions, sid)
	}
	return sessions, nil
}

func (s *InMemorySessionService) FindSimulation(ctx context.Context, sessionID string) (string, error) {
	if s == nil {
		return "", errors.New("session: nil service")
	}
	ctx, err := normalizeCtx(ctx)
	if err != nil {
		return "", err
	}
	if err := validateSessionID(sessionID); err != nil {
		return "", err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ns := s.namespaceForLocked(ctx, false)
	if ns == nil {
		return "", nil
	}
	ns.evictExpiredLocked(time.Now(), s.ttl)
	return ns.sessionSim[sessionID], nil
}

func (s *InMemorySessionService) ListSimulations(ctx context.Context) ([]string, error) {
	if s == nil {
		return nil, errors.New("session: nil service")
	}
	ctx, err := normalizeCtx(ctx)
	if err != nil {
		return nil, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ns := s.namespaceForLocked(ctx, false)
	if ns == nil {
		return nil, nil
	}
	ns.evictExpiredLocked(time.Now(), s.ttl)
	sims := make([]string, 0, len(ns.simSessions))
	for simID := range ns.simSessions {
		sims = append(sims, simID)
	}
	return sims, nil
}

func (s *InMemorySessionService) Flush(ctx context.Context) (int, error) {
	if s == nil {
		return 0, errors.New("session: nil service")
	}
	ctx, err := normalizeCtx(ctx)
	if err != nil {
		return 0, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	key := account.KeyPrefix(ctx)
	ns := s.namespaces[key]
	if ns == nil {
		return 0, nil
	}
	count := len(ns.agentTypes)
	delete(s.namespaces, key)
	return count, nil
}

// Reap evicts sessions older than the configured TTL in the caller's namespace.
func (s *InMemorySessionService) Reap(ctx context.Context) (int, error) {
	if s == nil {
		return 0, errors.New("session: nil service")
	}
	ctx, err := normalizeCtx(ctx)
	if err != nil {
		return 0, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ns := s.namespaceForLocked(ctx, false)
	if ns == nil {
		return 0, nil
	}
	return ns.evictExpiredLocked(time.Now(), s.ttl), nil
}
