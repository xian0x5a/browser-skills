---
status: done
---

# Mask password values in snapshots (issue #45)

## Goal

`thread.snapshot()` must never carry the value of an `<input type=password>`, or of an input or textarea the page hides with `-webkit-text-security`. A filled field shows the fixed mask `••••••••`, which hides the length and still tells a filled field from an empty one.

## Intention

Snapshots go to the model. The 1Password flow checks the snapshot after a fill, so a page that does not submit on its own would put the real password in the transcript. Masking belongs in the snapshot path because every caller goes through it.

## Scope & Constraints

- Fix the snapshot path in the Patchright bridge: the ARIA tree and its text fallbacks.
- Local fixture pages only; no Google traffic.
- `thread.evaluate` stays an explicit escape hatch and is out of scope.
- A real 1Password login is not exercised; the leak is in how a filled field renders, whatever filled it.

## Surprises & Discoveries

Probed on a local page with Patchright 1.64.1:

- The injected script prints `element.value` for an input under any role, usually `textbox`, `searchbox`, `combobox` or `spinbutton`. The tree has no input type, so the text alone cannot identify a password field.
- With a placeholder that differs from the name, the value moves to a nested `- text:` line under the node.
- A name that needs YAML quoting wraps the node in single quotes: `- 'textbox "Password: required" [ref=e5]': value`.
- Only interactable nodes get refs, so `pointer-events: none` prints a value with no ref.
- Refs resolve across frames and shadow DOM through `aria-ref=`; a stale ref times out and an unknown frame ref raises.
- `page.content()`, the last text fallback, holds a password in a `value` attribute (prefilled, or synced by a framework). `inner_text` never holds it.
- An accessible name can embed a password value. Playwright drops an ancestor's redundant name only when both nodes have refs, so a form locked with `pointer-events: none` during submit prints `row "Password hunter2"` above the field. A password field labelling another control (`aria-labelledby`, or a `<label for>` wrapping it) does the same.
- `<input type=password role=button>` also prints its value: any explicit role can carry one.
- `cloneNode` on the live document runs custom element constructors, so the HTML fallback copies into an inert document instead.
- Playwright's selector engine is slow here: counting `input[type=password i]` took 260 ms at 16,000 inputs, while a script that walks the DOM and reads computed styles took 20 ms and did not stall on a frame that never loads.
- `page.frames` updates only while the event loop runs, and the bridge runs it only inside a call. A frame attached between calls is missing until one round trip delivers the event.

## Decisions

Weighted options, scored 1-5:

| Criterion (weight)                            | Ask the browser per ref | Scrub known values from text | Patch the injected script |
| --------------------------------------------- | ----------------------- | ---------------------------- | ------------------------- |
| No leak, including during an async fill (40%) | 4                       | 2                            | 5                         |
| Page text stays accurate (25%)                | 5                       | 1                            | 5                         |
| Simplicity (20%)                              | 3                       | 4                            | 1                         |
| Survives Patchright upgrades (15%)            | 4                       | 5                            | 1                         |
| **Total**                                     | **4.05**                | **2.60**                     | **3.60**                  |

- Chosen: find the lines that print an input value, then ask the browser whether each ref is a password field. Identity needs no secret outside the browser and has no race with a fill, because a stale ref masks.
- Fail closed: on a page with a password field, no ref, a stale ref or a failed probe all mask. A text input without a ref loses its value there, which costs little because refless nodes cannot be targeted anyway.
- Rejected, scrubbing values: a short password rewrites refs and page text, the dots then reveal the password by position, and a value that changes between the two reads leaks.
- Rejected, patching the injected script: it forks the driver for every upgrade.
- Ancestor names: a masked line also masks the name of each ancestor whose role builds its name from content. It needs no secret, and author-given names such as `form "Login"` survive.
- Not covered: a password field inside the element that provides another node's name (`aria-labelledby`, `<legend>`, `<caption>`, `<figcaption>`, `<summary>`, a `<label>` for another control), when a missing ref keeps that name; and a password input under a role no text input uses. Both need odd markup. The text cannot tell the first from an author's name without masking those too, and probing every line for the second costs one round trip per paragraph.
- Probes share the snapshot's 3 s budget, so a stale ref can hold one snapshot for 3 s during navigation.
- Normal fields pay nothing (#48). Each probe costs about 1 ms, which reached 15 s at 16,000 filled text inputs on a page with no password. One scan per frame takes about 20 ms on that page, so probes now run only when a secret field exists. The scan runs before and after the ARIA read: only after would miss a field a submit removes, and only before would miss one rendered during the read. A timeout or an error fails closed. A field that exists only between the two scans stays a gap: it must render, hold a value and vanish within one read. On a hung page the scan adds up to 3 s.

T3 Code's browser (`ServerBrowserPage.ts`) was compared before settling #48. It scrubs known values, the option rejected above, and handles that option's weaknesses: it reads values before and after the tree and fails the snapshot if they changed, adds word boundaries for short values, and keeps roles and attributes out of the scrub. It also reaches accessible names in any position. Copying it would mean matching text and retrying when a value changes, so the per-ref probe stays.

Options for #48, scored 1-5:

| Criterion (weight)                | Tolerate | Count first | T3 value scrub |
| --------------------------------- | -------- | ----------- | -------------- |
| Normal fields do not pay (35%)    | 2        | 5           | 4              |
| No leak (35%)                     | 5        | 4           | 3              |
| Simplicity (20%)                  | 5        | 4           | 1              |
| Ordinary page latency (10%)       | 5        | 4           | 3              |
| **Total**                         | **3.95** | **4.35**    | **2.95**       |

After #48, wider coverage was scored 1-5. "Any-role names" masks every ref-less ancestor's name; "T3 scrub" also reaches names outside the ancestors:

| Criterion (weight)                | Neither  | CSS-masked fields | Plus any-role names | T3 scrub |
| --------------------------------- | -------- | ----------------- | ------------------- | -------- |
| Stops a realistic leak (35%)      | 2        | 4                 | 4                   | 5        |
| Simplicity (25%)                  | 5        | 4                 | 3                   | 1        |
| Page text stays accurate (20%)    | 5        | 5                 | 4                   | 2        |
| Normal pages do not pay (20%)     | 5        | 5                 | 5                   | 4        |
| **Total**                         | **3.95** | **4.40**          | **3.95**            | **3.20** |

- CSS-masked fields are in: the page hides the value on screen while the snapshot printed it, and scanning for them is faster than the selector count it replaced. A frame that navigates mid-read now fails closed by raising or by reaching the 3 s limit, where a selector count could return zero. A frame reloading every 40 ms can therefore stall a snapshot to the limit.
- The scan cannot see into closed shadow roots, unlike the selector it replaced. The tree cannot either, and a test pins that, so a driver upgrade that changes it fails loudly.
- Other accessible names stay out (#47): every remaining case needs odd markup, and fixing the ones outside the ancestors takes value scrubbing.
- Revealed passwords stay out (#46): a show-password toggle is an explicit reveal, and T3 does not cover it either.

## Work Plan

1. An independent agent writes regression tests from the contract and sees them fail.
2. Add the line finder and mask to `snapshots.py`, the identity probe to the bridge, and strip password `value` attributes from the HTML fallback.
3. Document the mask in the Python API and 1Password docs.
4. Independent review, then pin the runtime.

## Validation

- `SURF_TEST_LIVE_PATCHRIGHT=1 uv run pytest packages/surf-agent/tests -k password`
- `uv run pytest packages/surf-agent/tests`

## Progress

- Regression tests written first by an independent agent; five review rounds, with T3 Code's design compared before settling the approach; all checks below pass.

## Outcomes & Retrospective

Snapshots mask password and CSS-masked values in every field shape probed, in the ARIA tree, in enclosing content-named ancestors and in the HTML fallback. #48 then removed the cost for normal fields: a 16,000-input page without a password went from over 15 s to 0.53 s, and masking every line from 7.5 s to 3.4 ms. Review caught the ancestor-name leak after the first version passed its tests: masking one line was not enough, because the value also reaches names above it. The first custom element test could not fail, since `eval` does not see `window` state set by page scripts; a mutation check exposed it, and it now counts in the DOM.
