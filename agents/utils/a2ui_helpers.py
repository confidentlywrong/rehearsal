# Copyright 2026 Christian Haeussinger
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

def parse_markdown_blocks(text: str, prefix_id: str) -> tuple[list[dict], list[str]]:
    lines = text.strip().split("\n")
    components = []
    child_ids = []
    block_id = 0
    current_text = []

    def _flush_block():
        nonlocal block_id, current_text
        if not current_text:
            return
        text_str = "\n".join(current_text).strip()
        if not text_str:
            return
        c_id = f"{prefix_id}-block-{block_id}"
        block_id += 1
        usage_hint = "body"
        if text_str.startswith("#"):
            text_str = text_str.lstrip("#").strip()
            usage_hint = "h2"
        components.append({"id": c_id, "component": {"Text": {"text": {"literalString": text_str}, "usageHint": usage_hint}}})
        child_ids.append(c_id)
        current_text = []

    def _is_bullet(s: str) -> bool:
        s_strip = s.lstrip()
        if not s_strip:
            return False
        if s_strip[:2] in ("- ", "* ", "• "):
            return True
        head = s_strip.split(" ", 1)[0]
        if head.endswith(".") and head[:-1].isdigit():
            return True
        return False

    for line in lines:
        if line.startswith("#"):
            _flush_block()
            current_text.append(line)
            _flush_block()
        elif line.strip() == "":
            _flush_block()
        elif _is_bullet(line):
            _flush_block()
            bullet_text = line.lstrip().lstrip("-*•").lstrip()
            head = bullet_text.split(" ", 1)[0]
            if head.endswith(".") and head[:-1].isdigit():
                bullet_text = bullet_text.split(" ", 1)[1] if " " in bullet_text else bullet_text
            c_id = f"{prefix_id}-check-{block_id}"
            block_id += 1
            components.append({
                "id": c_id,
                "component": {
                    "CheckBox": {
                        "label": {"literalString": bullet_text.strip()},
                        "checked": {"literalBoolean": False},
                    }
                },
            })
            child_ids.append(c_id)
        else:
            current_text.append(line)

    _flush_block()
    return components, child_ids
