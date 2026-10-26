-- Copyright 2026 The Confidently Wrong Authors
--
-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at
--
--     http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.

-- Seed data for leadership_planner_with_memory agent.
-- Pre-loads 4 leadership scenarios matching the in-memory seed data.
--
-- P2 #11 (2026-06-20): seed rows are tagged with owner_id='__seed__'
-- (sentinel) so they are identifiable + filterable. The store's
-- per-user reads (owner_id=<uid>) already exclude these because the
-- sentinel never matches a real Firebase UID. Unscoped reads
-- (owner_id=None, single-tenant fallback) still surface them as
-- demo content. Operators who want to purge in prod can
--   DELETE FROM leadership_scenarios WHERE owner_id='__seed__';
-- This file is local-only (mounted into postgres init dir by
-- docker-compose); the prod cloud-sql-postgres schema migration
-- does NOT run these inserts.

INSERT INTO leadership_scenarios (scenario_id, scenario_data, created_at, eval_score, eval_result, owner_id)
VALUES
  ('seed-leadership-001',
   '{"scenario_id": "seed-leadership-001", "name": "Budget Crisis", "scenario_type": "crisis", "stakeholder_count": 5, "complexity": "complex", "summary": "A sudden 40% budget cut forces the leadership team to make rapid decisions about layoffs, project cancellations, and strategic pivots."}',
   '2026-01-01T00:00:00Z', 78,
   '{"overall_score": 78, "scores": {"stakeholder_alignment": 75, "decision_quality": 80, "resource_stewardship": 82, "crisis_readiness": 85, "strategic_coherence": 70, "ethical_alignment": 76}}',
   '__seed__'),

  ('seed-leadership-002',
   '{"scenario_id": "seed-leadership-002", "name": "Stakeholder Revolt", "scenario_type": "transformation", "stakeholder_count": 10, "complexity": "complex", "summary": "Key engineering leaders threaten to resign over a controversial AI adoption strategy, forcing the CEO to balance innovation with retention."}',
   '2026-01-02T00:00:00Z', 85,
   '{"overall_score": 85, "scores": {"stakeholder_alignment": 90, "decision_quality": 88, "resource_stewardship": 78, "crisis_readiness": 82, "strategic_coherence": 86, "ethical_alignment": 84}}',
   '__seed__'),

  ('seed-leadership-003',
   '{"scenario_id": "seed-leadership-003", "name": "Innovation Pivot", "scenario_type": "transformation", "stakeholder_count": 8, "complexity": "moderate", "summary": "The board demands a shift from on-premise to cloud-native AI, requiring massive retraining and infrastructure investment."}',
   '2026-01-03T00:00:00Z', 72,
   '{"overall_score": 72, "scores": {"stakeholder_alignment": 68, "decision_quality": 75, "resource_stewardship": 70, "crisis_readiness": 65, "strategic_coherence": 78, "ethical_alignment": 76}}',
   '__seed__'),

  ('seed-leadership-004',
   '{"scenario_id": "seed-leadership-004", "name": "Compliance Challenge", "scenario_type": "compliance", "stakeholder_count": 7, "complexity": "moderate", "summary": "New AI regulation requires comprehensive model governance, explainability, and bias auditing across all deployed systems."}',
   '2026-01-04T00:00:00Z', 80,
   '{"overall_score": 80, "scores": {"stakeholder_alignment": 78, "decision_quality": 82, "resource_stewardship": 75, "crisis_readiness": 80, "strategic_coherence": 84, "ethical_alignment": 81}}',
   '__seed__')

ON CONFLICT (scenario_id) DO NOTHING;


