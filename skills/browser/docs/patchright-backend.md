# Browser runtime

Read this for startup failures, profile/port overrides, or shutdown behavior. Surf drives a persistent Chrome-channel profile through Patchright and a local bridge.

## Setup

Install Google Chrome where Patchright's `chrome` channel finds it (its standard install location). Automation always launches that channel; `SURF_AGENT_CHROME_BIN` only changes the executable `Browser().open_profile()` starts, and it must be the same Chrome so both use one profile format. For Python dependency failures, read [launcher setup](launcher.md).

## Runtime data

- Storage: `config.json` (including [cookie import](cookie-import.md) settings) lives in the platform config directory, locks and search state in the state directory, and the profile in the data directory under `profiles/chrome/`. On Linux these follow `XDG_CONFIG_HOME`, `XDG_STATE_HOME` and `XDG_DATA_HOME`, so overriding `HOME` alone moves nothing while those are set.
- Cache: Chrome's HTTP cache and Code Cache for the profile live in the platform cache directory under `profiles/chrome/` (`~/.cache/surf-agent` on Linux, `~/Library/Caches/surf-agent` on macOS) and may be deleted at any time. Chrome's on-device AI model (Gemini Nano, about 4 GB) is disabled so it is not downloaded into the profile. A profile from before this change keeps its old `OptGuideOnDeviceModel`, `Default/Cache` and `Default/Code Cache`; delete them by hand while the bridge is stopped.
- `SURF_AGENT_HOME`: one directory that replaces all three, so config, state and the profile (`$SURF_AGENT_HOME/profiles/chrome`) move together, with the cache in `$SURF_AGENT_HOME/cache`. A new location starts with an empty profile and without the user's cookie-import config.
- `SURF_AGENT_PROFILE_DIR`: overrides the profile. Its cache stays inside it, as Chrome lays it out.
- `SURF_AGENT_PORT`: bridge port, default `9346`.
- `SURF_AGENT_APP_ID`: Wayland app-id and X11 WM_CLASS of Surf's Chrome windows, default `surf-agent`.

Inspect actual settings with `Browser().profile()`.

## Browser behavior

The bridge uses Patchright's async API with a persistent asyncio runner and a persistent Chrome context, without a fixed viewport. Color-scheme emulation is reset so pages follow the desktop theme. Before each launch the bridge clears the zoom saved in the profile (default and per-site): under any zoom but 100%, screenshots show only part of the viewport, or nothing once scrolled, and Chrome has no supported command to reset zoom. The application identifier uses `--name=<app_id>` so Chromium does not mistake it for a page target.

Response capture (`thread.responses()`) listens on the browser context, so a thread window's first load is captured even though the window is created through raw CDP. Bodies stay in bridge memory, capped per page, are cleared on `open()` and bridge stop, and are released after the page closes. It adds no detection surface: Patchright already enables the CDP `Network` domain on every page, and reading a body is a browser-side call page scripts cannot observe.

Chrome-extension behavior depends on the installed browser and profile; verify [1Password setup](1password-setup.md) with a live check rather than assuming extension compatibility.

## Cookies and shutdown

[Cookie import](cookie-import.md) refreshes configured login state only before starting an inactive profile. Patchright excludes its `--password-store=basic` and `--use-mock-keychain` defaults so imported cookies decrypt with Chrome's real OS password store (Linux) or Keychain (macOS).

Chrome starts without a window and every thread window opens in the background, so Surf does not take focus; `Thread(name).focus()` raises one on request.

Closing the final user-visible page requests bridge shutdown after the close response; background workers do not count. Chrome may independently close the persistent context, causing the bridge to exit. Subsequent startup repeats lifecycle checks. Handle interrupted actions through the [recovery workflow](../SKILL.md#recovery).
