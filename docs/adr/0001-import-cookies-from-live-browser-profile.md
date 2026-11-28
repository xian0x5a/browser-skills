---
status: accepted
---

# Import cookies from a live browser profile

Surf Agent will refresh authentication by importing cookies from an explicitly configured live browser profile into its inactive Chrome-family profile. V1 supports Linux, requires the same browser family, OS user, and matching encryption metadata, and uses SQLite online backup so the locked source browser remains running. Imports are limited to an explicit domain allowlist or explicit all-domain consent and transactionally upsert matching cookies without deleting destination-only cookies.

## Considered options

Full profile copying was rejected because browser storage mixes authentication with large caches, offline application data, history, extensions, and unrelated private state. Filesystem snapshots and process suspension were rejected because cookie-only import can use SQLite's supported online backup mechanism without filesystem dependencies or interrupting the source browser. A browser-extension control path was rejected because cookie refresh must remain part of the existing AXI/Patchright profile workflow rather than introduce another backend.

## Consequences

The configured source is fingerprinted using its Cookies database and WAL/journal metadata; automatic import runs only after observable change, while an explicit import always runs. Cookie import is opt-in, preserves destination-only sessions for convenience, and aborts backend startup on validation or compatibility failure. When no user-visible pages remain, AXI stops its bridge after a two-second recheck; Patchright stops immediately after returning the close response because Chrome may already have closed its persistent context. Closed Patchright contexts restart through lifecycle preflight and retry the interrupted command once. Automated coverage uses deterministic SQLite and lifecycle tests; real-browser smoke testing remains optional.

## Amendment: exclude Google sign-in by default

A scope may name excluded domains, which subtract from either form, and every scope excludes Google sign-in hosts (`google.<public suffix>` and `youtube.com`, with subdomains) unless it sets `exclude_google: false`. On Linux, an all-domain import from a Chrome signed in to two Google accounts signed that Chrome out of Google twice, each time right after Surf started, while Surf's profile held the full Google cookie set. The mechanism is inferred, not captured: copying the cookies forks one server-side Google session, and Surf's Chrome, signed in to only one of the accounts, reconciles its web accounts through Google or rotates the shared session token, changing the session the source depends on. Key change and device-bound sessions were ruled out. Without Google cookies, Surf's Chrome holds its own session from its own sign-in.

The exclusion covers allowlists too, because the harm comes from the cookies, not the scope form, and `import_cookies_for` would otherwise add `google.com` after a consent prompt that never mentions the sign-out. A requested allowlist domain that exclusions cover in full is rejected rather than silently importing nothing. Existing configurations load with the exclusion on, including allowlists that already name a Google domain: that entry imports nothing, and `import_cookies_for` drops it the next time it extends the allowlist. The changed scope changes the source fingerprint, so the next start re-imports, but cookies imported earlier are not removed.

## Amendment: macOS, not Windows

macOS is supported with the same verbatim row copy: Chrome keeps one cookie key per app in the Keychain item `Chrome Safe Storage`, and Surf's destination is the same signed Chrome app, so it reads that key. Windows is unsupported: a running Chrome holds its Cookies database under a mandatory Windows file lock, so the online backup this design relies on cannot read it without closing the source browser.

Verified on macOS 27 with Chrome 153: an imported GitHub session authenticates in Surf. Two macOS differences follow. Chrome writes no `os_crypt` to `Local State`, so absent metadata is compared like any other value and matches only absence. macOS also denies Chrome's data directory to other apps, so the app running Surf needs a one-time permission to access other apps' data. A Chrome launched from an SSH session cannot read the locked Keychain and drops the imported rows, so verification must run in the logged-in desktop session.
