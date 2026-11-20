// Copyright 2026 Christian Haeussinger. Apache-2.0.

package account

import (
	"encoding/json"
	"fmt"
)

// encodeJSON serializes the audit metadata map, returning nil when empty
// (so callers pass SQL NULL) and propagating any marshal error.
func encodeJSON(m map[string]any) (any, error) {
	if len(m) == 0 {
		return nil, nil
	}
	b, err := json.Marshal(m)
	if err != nil {
		return nil, fmt.Errorf("account: encode json metadata: %w", err)
	}
	return b, nil
}

// decodeJSON deserializes audit metadata JSON bytes, returning an error
// on malformed JSON instead of silently degrading to an empty map.
func decodeJSON(b []byte) (map[string]any, error) {
	if len(b) == 0 {
		return nil, nil
	}
	var out map[string]any
	if err := json.Unmarshal(b, &out); err != nil {
		return nil, fmt.Errorf("account: decode json metadata: %w", err)
	}
	if out == nil {
		return map[string]any{}, nil
	}
	return out, nil
}
