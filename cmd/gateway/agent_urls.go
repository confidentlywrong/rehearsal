// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.
//
// SSRF guard for AGENT_URLS (audit P3 #9, 2026-06-21).
//
// The gateway POSTs to whatever URLs the operator put in the
// AGENT_URLS env var. The values are operator-controlled (not
// user-controlled), so the worst case is a tfvars typo, not a
// hostile request — but a typo that points one of these URLs at
// http://169.254.169.254/... would have the gateway happily fetch
// the GCP instance metadata service (token, service-account info)
// and forward it to the "agent" side. That is a meaningful
// blast-radius increase for an otherwise low-impact typo.
//
// This file is a small, allocation-free validator that runs once
// at startup. It is deliberately conservative:
//
//   - GCP metadata DNS aliases ("metadata.google.internal",
//     "metadata") are HARD-BLOCKED before DNS resolution. The
//     literal IP 169.254.169.254 is caught by the link-local
//     check below; the alias must be caught separately because a
//     /etc/hosts mapping could shadow it.
//
//   - In cloud (`K_SERVICE != ""`), only `https://` URLs are
//     accepted. The single exception is loopback URLs, which are
//     occasionally used by an in-process test agent and pose no
//     SSRF risk. Cloud Run service URLs and internal load
//     balancers are HTTPS-only by default, so an `http://` agent
//     URL on Cloud Run is always a misconfiguration.
//
//   - For every URL, the hostname is DNS-resolved and each
//     returned IP is checked. ANY result in 169.254.0.0/16
//     (link-local) is rejected — that range exists specifically
//     for cloud-metadata endpoints across every major provider,
//     so a legitimate intra-VPC agent will never resolve into it.
//
// RFC1918 ranges (10/8, 172.16/12, 192.168/16) and loopback
// (127/8) are NOT blocked: Cloud Run with a VPC connector
// reaches internal services via RFC1918, and local-dev agent
// processes bind to 127.0.0.1. Blocking either would defeat the
// intended deployment topology.
//
// Validation is per-URL. If at least one URL survives, boot
// continues with the survivors — a single bad entry in
// AGENT_URLS does not take the whole gateway down. If every URL
// fails AND the list was non-empty, the operator has zero usable
// agents; we log the dropped list and continue (the catalog will
// /healthz as zero-agent and existing `503 "failed to discover
// any agents"` logic kicks in).

package main

import (
	"errors"
	"fmt"
	"net"
	"net/url"
	"strings"
)

// validateAgentURL returns nil when rawURL is safe for the gateway
// to call as an agent endpoint. `inCloud` is true on Cloud Run
// (i.e. when K_SERVICE is set); it tightens the scheme check.
//
// The `lookupIP` parameter is injected to make the function
// testable without DNS. Production callers should pass
// `net.LookupIP`.
func validateAgentURL(rawURL string, inCloud bool, lookupIP func(string) ([]net.IP, error)) error {
	u, err := url.Parse(rawURL)
	if err != nil {
		return fmt.Errorf("parse: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return fmt.Errorf("unsupported scheme %q (want http or https)", u.Scheme)
	}
	host := u.Hostname()
	if host == "" {
		return errors.New("missing host")
	}

	// Hard-block GCP metadata aliases by name before DNS, so a
	// /etc/hosts entry can't shadow the check.
	lowerHost := strings.ToLower(host)
	if lowerHost == "metadata.google.internal" || lowerHost == "metadata" {
		return fmt.Errorf("host %q is the GCP metadata alias", host)
	}

	// Cloud-mode scheme tightening: https-only except loopback.
	if inCloud && u.Scheme != "https" {
		if ip := net.ParseIP(host); ip == nil || !ip.IsLoopback() {
			return errors.New("scheme http not allowed in cloud (use https)")
		}
	}

	// If the host is a literal IP, check it directly — no DNS round
	// trip needed and lookupIP would just round-trip the literal.
	if ip := net.ParseIP(host); ip != nil {
		if ip.IsLinkLocalUnicast() {
			return fmt.Errorf("host %q is link-local (SSRF risk)", host)
		}
		return nil
	}

	// DNS-resolve and check every returned IP. We never permit a
	// link-local result, even if the hostname looks innocuous.
	ips, err := lookupIP(host)
	if err != nil {
		// Resolution failure is a different class of problem
		// (missing DNS / typo) — surface it so the operator can
		// fix it, but distinguish it from the SSRF rejection.
		return fmt.Errorf("dns resolve %q: %w", host, err)
	}
	if len(ips) == 0 {
		return fmt.Errorf("dns resolve %q returned no addresses", host)
	}
	for _, ip := range ips {
		if ip.IsLinkLocalUnicast() {
			return fmt.Errorf("host %q resolves to link-local %s (SSRF risk)", host, ip)
		}
	}
	return nil
}
