# Native preview

Run the checkout QML on an Omarchy desktop with its installed Quickshell and
shared shell components:

```sh
python3 scripts/preview-plugin.py --page settings
# Preview another checkout without copying infrastructure into it:
python3 scripts/preview-plugin.py --repo /path/to/checkout --page settings
```

The launcher creates a private temporary configuration, copies `BarWidget.qml`,
`LimitModel.js` and assets, and links the installed `Commons` and `Ui` modules.
It replaces the Python bridge with `tests/fixtures/preview_bridge.py`. All
accounts use `example.invalid` addresses. The fixture has no network, service,
clipboard, provider, installation or real configuration operations.

The separate preview bar and popup briefly take native keyboard focus. They use
the actual Omarchy `Panel`, `KeyboardPanel`, `BarIconButton` and control types.
The existing shell stays running. Press Ctrl+C in the launching terminal to
stop the preview. Use `--duration 30` to stop it automatically. Temporary files
are removed on exit unless `--keep` is supplied. `--keep` retains the copied
source, fixture state, command trace, log and smoke capture in a private `/tmp`
directory; remove that directory after inspecting it.

The launcher prints its configuration path and a scoped IPC command. Always
pass that exact path when addressing the preview. For example:

```sh
quickshell ipc -p /tmp/omaproxy-preview-EXAMPLE call soojy.omaproxy showPage accounts
quickshell ipc -p /tmp/omaproxy-preview-EXAMPLE call omaproxy-preview controls
quickshell ipc -p /tmp/omaproxy-preview-EXAMPLE call omaproxy-preview state
quickshell ipc -p /tmp/omaproxy-preview-EXAMPLE call omaproxy-preview quit
```

The plugin IPC target keeps its normal name but configuration selection isolates
it from the installed plugin. The extra `omaproxy-preview` target belongs only
to the temporary host. Its `activate` function invokes one enabled, visible
native button's click handler by exact label. Its `capture` function captures
the rendered popup card through Qt's `grabToImage`; it excludes unrelated
windows and desktop content.

## Repeatable verification

```sh
python3 scripts/preview-plugin.py --smoke --keep
```

The smoke lane waits for fixture status and switches through native tab controls.
It detects the feature set in the checkout and verifies the corresponding
handlers and state transitions:

- Backend update check, reviewed fixture install and restore.
- Weighted routing, conversation affinity, subagent affinity, cooldown toggles,
  duration and retry edits, rejecting blank or fractional retry inputs before
  invoking the bridge, quota alert opt-in and opt-out.
- Read-only diagnostics refresh and explicit fixture activity capture.
- Provider discovery, editing JSON model aliases, preserving credential counts,
  credential weights, staged removal, confirmation reset on page changes,
  confirmed removal and creation with dummy credentials.
- Remote connection save with dummy keys, saved client-key removal and local
  connection selection.

It closes and reopens Accounts, checks that the email reveal state is empty,
captures the rendered cards, waits for complete PNG files, and checks the
fixture command trace. It exits nonzero if a control, state transition or
capture is missing. Field values use percent-encoded IPC transport so brackets
in JSON aliases arrive intact. The native form still parses those values and
submits its own normal stdin payload to the fixture.

Inspect the retained PNG files and `quickshell.log` before treating the visual
check as complete. All contracts return synthetic values. Quota alert settings
use the temporary bar host; the fixture never sends desktop notifications.

This lane verifies QML loading, native rendering, binding and action wiring,
and concealment after reopening. It does not verify live upstream responses,
downloads, service restarts, billing totals, provider delivery, clipboard
contents or pointer hit testing. Prefer AT-SPI inspection when available; on
Quickshell 0.3.1 here the Qt application exposes no top-level AT-SPI windows.
The scoped native control bridge keeps the lane executable despite that gap.

Recorded validation on the development desktop used Quickshell 0.3.1 and the
installed Omarchy components. The updater, controls and remote smoke lanes
passed; their cards were visually inspected. The logs contained a host portal
registration warning and no QML
errors. The fixture never executed the real backend bridge.
