// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

package account

import "encoding/json"

// encodeJSON serializes the audit metadata map. Returns nil (so the
// caller passes SQL NULL) on failure instead of panicking — losing
// the metadata blob is preferable to losing the whole audit row.
func encodeJSON(m map[string]any) any {
	if len(m) == 0 {
		return nil
	}
	b, err := json.Marshal(m)
	if err != nil {
		return nil
	}
	return b
}

// decodeJSON is the inverse. On malformed input it returns an empty
// map so handlers can iterate the result without a nil check.
func decodeJSON(b []byte) map[string]any {
	out := map[string]any{}
	_ = json.Unmarshal(b, &out)
	return out
}
