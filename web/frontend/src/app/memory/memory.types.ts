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

/**
 * Copyright 2026 Google LLC. Apache-2.0.
 *
 * TypeScript mirror of the Go-side DTOs from cmd/gateway/memory.go.
 * Field names match the JSON wire format byte-for-byte (snake_case)
 * — do NOT camelCase these. If the Go side adds or renames a field,
 * update this file in the same commit.
 */

export interface Analysis {
  id: string;
  operator_id: string;
  org_name: string;
  title: string;
  description?: string;

  created_at: string;
}

export interface MemoryDocument {
  id: string;
  analysis_id: string;
  kind: string;
  filename: string;
  sha256: string;
  storage_uri: string;
  byte_size: number;
  ingested_at: string;
}

