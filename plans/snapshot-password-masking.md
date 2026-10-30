---
status: done
---

# Mask password values in snapshots (issue #45)

## Goal

`thread.snapshot()` must never carry the value of an `<input type=password>`. A filled field shows the fixed mask `••••••••`, which hides the length and still tells a filled field from an empty one.

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
- Fail closed: no ref, a stale ref or a failed probe all mask. A text input without a ref loses its value in the snapshot, which costs little because refless nodes cannot be targeted anyway.
- Rejected, scrubbing values: a short password rewrites refs and page text, the dots then reveal the password by position, and a value that changes between the two reads leaks.
- Rejected, patching the injected script: it forks the driver for every upgrade.
- Ancestor names: a masked line also masks the name of each ancestor whose role builds its name from content. It needs no secret, and author-given names such as `form "Login"` survive.
- Not covered: a password field inside the element that provides another node's name (`aria-labelledby`, `<legend>`, `<caption>`, `<figcaption>`, `<summary>`, a `<label>` for another control), when a missing ref keeps that name; and a password input under a role no text input uses. Both need odd markup. The text cannot tell the first from an author's name without masking those too, and probing every line for the second costs one round trip per paragraph.
- Probes share the snapshot's 3 s budget, so a stale ref can hold one snapshot for 3 s during navigation. Probe time grows faster than the input count: about 5.6 s at 8,000 filled inputs and 17 s at 16,000, past the 15 s command timeout. Accepted, because only huge grids reach it.

## Work Plan

1. An independent agent writes regression tests from the contract and sees them fail.
2. Add the line finder and mask to `snapshots.py`, the identity probe to the bridge, and strip password `value` attributes from the HTML fallback.
3. Document the mask in the Python API and 1Password docs.
4. Independent review, then pin the runtime.

## Validation

- `SURF_TEST_LIVE_PATCHRIGHT=1 uv run pytest packages/surf-agent/tests -k password`
- `uv run pytest packages/surf-agent/tests`

## Progress

- Regression tests written first by an independent agent; two review rounds; all checks below pass.

## Outcomes & Retrospective

Snapshots mask password values in every field shape probed, in the ARIA tree, in enclosing content-named ancestors and in the HTML fallback. Review caught the ancestor-name leak after the first version passed its tests: masking one line was not enough, because the value also reaches names above it. The first custom element test could not fail, since `eval` does not see `window` state set by page scripts; a mutation check exposed it, and it now counts in the DOM.
