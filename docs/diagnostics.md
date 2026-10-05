# Diagnostics and telemetry scope

`diagnostics.snapshot(api, accounts=None, salt=None, consume_queue=False)` returns a bounded, redacted snapshot. Its adapter accepts full management paths and a `timeout` keyword, performs a GET, and raises an HTTP error for unsuccessful responses. Each call uses a two-second timeout. Up to four calls probe usage and accounts; an explicit queue capture can add two calls. The bridge rejects JSON response bodies above 2 MiB before decoding them.

The return value contains `usage`, `accounts`, `queue`, `client_attribution`, and `limitations`. Usage and accounts contain `availability`, `source`, `error`, `records`, `retained`, `omitted`, and `invalid`. Supported records contain anonymous `label`, `provider`, `success`, `failed`, and `recent_requests`. No overall activity total is inferred from an incomplete population. Counts are cumulative backend attempt counters; they do not count unique client requests or prove billing charges. Retention is limited to 128 records and 20 recent buckets per record. Unknown values are not replaced with zero.

Pass the already fetched raw auth-file list through `accounts` to avoid fetching it again. Pass a private, stable installation salt (for example, the existing management credential, kept in Python) through `salt` to keep labels stable across bridge invocations. The salt never enters the result. Without it, labels remain stable only within the Python process. Resetting the salt changes every anonymous label. The helper writes no files and retains no request history.

## Verified routes and schemas

The implementation was checked against these exact upstream tags:

- [v7.2.154](https://github.com/router-for-me/CLIProxyAPI/tree/v7.2.154), commit `ba7e55836dee959e93ec6d41395865d9ec535086`.
- [v8.0.13](https://github.com/router-for-me/CLIProxyAPI/tree/v8.0.13), commit `d7914afdedca7af95ee974a42453dc49fc1388ce`.

| Data | v8 route | Legacy route | Meaning |
| --- | --- | --- | --- |
| Upstream API-key counters | `/v8/management/observability/usage/api-keys` | `/v0/management/api-key-usage` | Upstream API-key accounts grouped by provider and `base_url\|api_key`; excludes OAuth credentials |
| Account counters | `/v8/management/credentials` | `/v0/management/auth-files` | `files` array of runtime account records with `auth_index`, provider, success, failed and recent requests |
| Pending receipts, explicit capture only | `/v8/management/observability/usage/queue?count=50` | `/v0/management/usage-queue?count=50` | Removes up to 50 records from the shared usage queue |

The API-key usage endpoint does **not** count client access keys. A zero-length result does not prove that an OAuth account received no traffic. Account counters are useful for OAuth coverage but do not establish which account handled a particular client request. The legacy `/v0/management/usage` route is not used: it is absent in both inspected tags, and an installed backend returning 404 is not evidence that enabling usage statistics restores that route.

The usage response is a provider-keyed map whose second-level keys contain upstream secrets. The helper replaces every composite key with an anonymous label. Account responses contain private filenames and emails; those fields are dropped. Known provider IDs use a fixed allowlist. Custom provider names are anonymous. Recent bucket labels accept only `HH:MM-HH:MM`, use backend local time, and contain neither dates nor a timezone.

Primary source:

- [v8 API-key usage handler](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.13/internal/api/handlers/management/api_key_usage.go), also present in v7.2.154.
- [v8 credential response builder](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.13/internal/api/handlers/management/auth_files.go), also contains counters in v7.2.154.
- [v8 management routes](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.13/internal/api/server_management_v8.go) and [v7 legacy routes](https://github.com/router-for-me/CLIProxyAPI/blob/v7.2.154/internal/api/server_management.go).
- [v8 queue handler](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.13/internal/api/handlers/management/usage.go) calls `PopOldest`; this is also true in v7.2.154.
- [v8 queue receipt schema](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.13/internal/redisqueue/plugin.go).
- [Recent bucket schema](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.13/sdk/cliproxy/auth/types.go): 20 buckets of 10 minutes each.

## Explicit queue capture

Automatic snapshots never read the queue. Although its HTTP method is GET, the backend removes records as it returns them. A separate action may call `snapshot(..., consume_queue=True)` only after explaining that other collectors lose access to the consumed events. This is a capture of currently pending receipts, not a request log, a queue peek, a continuous monitor, or a complete history. An empty queue may mean no recent records, disabled statistics, disabled queue publishing, expired retention, or that another consumer already removed them.

The capture returns only the current snapshot, with at most 50 events. `sanitize_events(items, salt=...)` applies the same boundary to records supplied by another authorized consumer without making requests. Client labels come only from a receipt's `api_key`; account labels come only from `auth_index`. No attribution is inferred from account eligibility, source filenames, configured provider keys, or user agents. Request and execution labels preserve only grouping relationships. Model names become anonymous labels because arbitrary model strings can contain private text.

For a returned array, `retained` counts supported displayed receipts, `invalid` counts unsupported records among the newest 50 inspected records, and `omitted` counts older records beyond that inspection limit. These populations are separate: `retained + invalid + omitted` equals the number of records returned by the backend. A capture with some supported receipts remains `available` and reports its invalid count. A nonempty inspected population with no supported receipts reports `unknown`, while an empty array remains `available` with zero receipts.

`capture_requested` records the explicit capture action. `consumed` means the queue request succeeded, even if its response could not be parsed or all receipts were invalid; it does not mean receipts were retained or identified. Failed requests report `consumed: false`. `client_attribution` is `receipt_fields_only` only when at least one retained receipt supplies a valid `api_key` that becomes a `client_label`. Supported account or request fields alone leave it `unavailable`, and one identified receipt does not identify every other receipt.

Outcome, HTTP status, latency, time to first token (TTFT), and allowlisted token counts appear only when the receipt supplies correctly typed fields. A backend zero remains a backend zero; the helper does not prove whether it was measured or a backend default. Missing fields are omitted, not estimated. The inspected receipt schema does not provide a retry count, so the helper does not invent one. Failure bodies, headers, response bodies, prompts, messages, tool data, email addresses, raw access keys, auth filenames, network addresses and arbitrary error text never enter display output.

## Availability and verification

`available` means the response matched the inspected schema, even if the supported population is empty. `unsupported` means both route variants returned 404, 405, or 501. `unavailable` means another request failure, including denied access or a timeout; it does not trigger another route probe. `unknown` means the payload schema could not be recognized. Default queue snapshots report `read_only_unavailable` because reading that endpoint has a side effect. Exception messages and backend error bodies are replaced by fixed messages.

The tests use fake responses, HTTP failures and hostile secret-bearing fields. They check endpoint fallback, missing capabilities, OAuth/account separation, malformed counts, receipt attribution, output bounds, stable labels, exception redaction, and explicit queue capture. Capture regressions distinguish retained, invalid and omitted records; verify that an all-invalid response reports an unknown schema; and keep capture success separate from client attribution. Run:

```sh
python3 -m unittest discover -s tests -p test_diagnostics.py -v
```

These tests verify the local parsing and privacy boundary. They do not prove telemetry publishing or queue retention on an installed backend, and no live inference is needed to run them.
