-- Copyright 2026 Christian Haeussinger
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

-- migrations/0005_analyses_org_name_nullable.sql
--
-- Make org_name optional to support the single-user notepad-style memory persona.

BEGIN;

ALTER TABLE analyses ALTER COLUMN org_name DROP NOT NULL;

COMMIT;
