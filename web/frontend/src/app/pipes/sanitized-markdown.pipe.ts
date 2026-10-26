/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { Pipe, PipeTransform, SecurityContext, inject } from '@angular/core';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { MarkdownService } from 'ngx-markdown';

/**
 * SanitizedMarkdownPipe — defense-in-depth wrapper around ngx-markdown.
 *
 * The built-in `MarkdownPipe` from ngx-markdown converts markdown to HTML
 * via `marked`, sanitises with `DomSanitizer.sanitize(SecurityContext.HTML)`,
 * then wraps the result with `bypassSecurityTrustHtml()` so Angular does
 * not double-sanitise when bound to `[innerHTML]`.
 *
 * Problem: the `bypassSecurityTrustHtml` call means Angular's built-in
 * sanitiser is *disabled* for that binding.  If ngx-markdown's internal
 * sanitisation changes (library update, config drift, or a bug in
 * `marked`), the app is one step away from XSS via agent-generated
 * content (see security audit Finding #4, 2026-07-02).
 *
 * This pipe provides an *explicit, owned* sanitisation step:
 *
 *   1. Delegates to `MarkdownService.parse()` (markdown → HTML).
 *   2. Runs the HTML through `DomSanitizer.sanitize(SecurityContext.HTML)`,
 *      which strips `<script>`, event handlers, `javascript:` URIs, etc.
 *   3. Wraps the clean HTML with `bypassSecurityTrustHtml()` so Angular
 *      renders it as-is.
 *
 * Even if ngx-markdown drops its internal sanitisation in a future
 * version, *our* explicit call at step 2 still protects the output.
 *
 * Usage (template):
 *   [innerHTML]="text | sanitizedMarkdown | async"
 */
@Pipe({
  name: 'sanitizedMarkdown',
  standalone: true,
})
export class SanitizedMarkdownPipe implements PipeTransform {
  private readonly domSanitizer = inject(DomSanitizer);
  private readonly markdownService = inject(MarkdownService);

  async transform(value: string | null | undefined): Promise<SafeHtml> {
    if (value == null || value === '') {
      return '';
    }
    // Step 1: markdown → raw HTML (via marked).
    const rawHtml = await this.markdownService.parse(value);

    // Step 2: explicit sanitisation — strips dangerous elements/attributes.
    const clean = this.domSanitizer.sanitize(SecurityContext.HTML, rawHtml) ?? '';

    // Step 3: mark as trusted so [innerHTML] renders it without
    // Angular's automatic (and now redundant) re-sanitisation.
    return this.domSanitizer.bypassSecurityTrustHtml(clean);
  }
}
