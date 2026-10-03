# Routing and custom providers

OmaProxy reads backend capabilities before offering routing controls. Discovery
uses management GET requests and returns only an allowlist of display values.
It does not save config, reset cooldowns, or call a model.

## Backend contracts

The implementation was checked against CLIProxyAPI tags
[v7.2.154](https://github.com/router-for-me/CLIProxyAPI/tree/v7.2.154) and
[v8.0.13](https://github.com/router-for-me/CLIProxyAPI/tree/v8.0.13).
Both accept `round-robin`, `weighted-round-robin`, and `fill-first` through
`/v0/management/routing/strategy`. Weighted routing is offered for a known
compatible backend version, or when the backend reports that it is already
using that strategy. Unknown versions retain round-robin and fill-first.

| Control | v7.2.154 | v8.0.13 |
| --- | --- | --- |
| Strategy | Scalar PATCH | Routing subtree PATCH |
| Retry rounds, credential cap, maximum wait | Scalar PATCH | Routing subtree PATCH |
| Session affinity, TTL, subagent inheritance | Read only | Routing subtree PATCH |
| Disable cooling, persist cooldown state | Read only | Routing subtree PATCH |
| Custom provider create | Locked list GET and PUT | Locked list GET and PUT via v0 compatibility API |
| Custom provider edit | Exact-name PATCH | Exact-name PATCH via v0 compatibility API |
| Custom provider remove | Exact-name DELETE | Exact-name DELETE via v0 compatibility API |

The v7 config contains affinity fields, but its management API has no narrow
mutation route for them. OmaProxy does not replace full config JSON or rewrite
YAML to work around that gap. On v8, it merges only requested fields into
`/v8/management/config/routing`; backend tests verify that comments and unrelated
settings survive. A successful v8 mutation can migrate a legacy config document
to the backend's v8 layout. GET discovery does not migrate it.

The verified source contracts are in `internal/api/server_management.go`,
`internal/api/handlers/management/config_basic.go`, and
`internal/api/handlers/management/config_lists.go`. V8 adds
`internal/api/server_management_v8.go` and
`internal/api/handlers/management/config_v8.go`.

## What the choices mean

- Round-robin rotates eligible credentials for the requested model.
- Fill-first favors the first eligible credential until it becomes unavailable.
- Weighted round-robin distributes eligible credentials in proportion to their
  configured weights. An omitted credential weight defaults to 1; 0 excludes
  that credential. The maximum weight is 1,000,000. Higher priority still takes
  precedence over weight. Weight is not a model substitution setting.
- Session affinity binds an identified session to its credential and fails over
  when that credential becomes unavailable. Its default TTL is one hour.
  Subagent inheritance keeps a child session with its parent's credential;
  disabling inheritance lets the fallback selector distribute child sessions.

The UI exposes bounded edits: 0–10 additional retry rounds, 0–100 retry
credentials, 0–300 seconds of maximum cooldown wait, and a TTL between one second
and 24 hours. A credential cap of 0 uses the backend's unlimited default; the
round and wait limits are separate. These limits constrain new edits; discovery
still shows existing values outside them. Disabling cooling changes failure
handling, so OmaProxy only does it after an explicit settings change.

Routing helpers never edit model aliases, quota-exceeded model switches,
credential priority, or requested model IDs. Existing backend rules for those
settings still apply. The caller holds the shared management mutation lock.
V8 applies all requested routing fields in one PATCH. V7 scalar writes are
sequential; a failure identifies fields already applied and asks for a refresh.
Every successful operation reads back the changed values before reporting
success.

## Custom provider operations

Provider names are exact targets and cannot be renamed by the edit action.
Names created or managed here use letters, numbers, underscores, or hyphens,
up to 60 characters. Duplicate names block edits and removals.

Display results contain only name, validated base URL, model names and aliases,
disabled state, credential count, credential indexes, whether a key exists, and
weights. They exclude API keys, headers, proxy settings, and raw config.
Credential form values travel through stdin into Python and management request
bodies; they do not appear in command arguments or display JSON.

An edit sends only supplied fields. A blank key preserves all existing secrets.
Replacing a key or changing its weight preserves other credential fields; a
provider with multiple credentials requires an explicit credential index.
Setting weight to JSON `null` restores the backend default. Editing a model
that retains its name and alias preserves its existing capability metadata.
Creation requires at least one explicit model ID, and remote providers require
a key. A localhost provider may use no key. Removal deletes only the exact name.

The v0 API has no append endpoint. Creation therefore reads the current provider
list and writes the preserved list plus the new definition while holding the
same local lock as other config changes. This lock coordinates OmaProxy commands;
it cannot prevent an independent management client from editing between those
requests. Edits and deletions use target-specific endpoints and do not replace
unrelated provider definitions.

## Explicit provider tests

A provider test performs exactly one `GET <base-url>/models` using the selected
credential. It does not infer a model, change configured aliases, or make a
completion request. A successful model catalog proves discovery at that endpoint;
it does not prove inference, quota, or compatibility with another protocol.
Custom header configurations are unsupported by this test and return a clear
error before sending a request.

Base URLs reject embedded credentials, query parameters, fragments, whitespace,
control characters, and remote HTTP. HTTP is accepted only for `localhost`,
`127.0.0.1`, or `::1`. The caller's request function must disable redirects and
HTTP proxy inheritance so credentials cannot follow a redirect. Provider error
responses are reduced to an HTTP status or a generic failure message.

## Verification

`python -m unittest discover -s tests -p 'test_routing.py' -v` and the equivalent
command for `test_providers.py` run mock contract tests. Set
`OMAPROXY_TEST_BINARY=/path/to/cli-proxy-api` to also run isolated backend tests.
They create a temporary config and listener, start a separate backend process,
and terminate it afterwards. They verify narrow writes, retained provider
secrets, exact removal, and comment preservation. They do not change the live
service, live accounts, or production config, and make no inference requests.
