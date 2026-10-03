# Coding client compatibility checks

OmaProxy exposes CLIProxyAPI's OpenAI-compatible endpoint to coding clients.
These checks cover Responses HTTP/SSE and downstream WebSocket contracts, then
optionally run real Codex clients against the same isolated proxy. They use a
local fake Chat Completions upstream. No prompt goes to a model provider.

## Run the protocol checks

Use a trusted CLIProxyAPI binary and Python 3.11 or newer:

```bash
OMAPROXY_TEST_BINARY=/absolute/path/to/cli-proxy-api \
  python3 -m unittest discover -s tests -p test_responses_integration.py -v
```

The suite starts a fresh backend for each test, with an ephemeral loopback port,
a temporary configuration and auth directory, fake keys, and `--local-model`.
It does not use or restart the installed proxy service. The upstream emits Chat
Completions chunks; the real Go backend must translate them into Responses.

The 11 checks assert:

| Contract | Evidence |
| --- | --- |
| Authentication and model aliases | Incorrect client key returns 401 before reaching upstream; model alias and instructions reach the correct upstream with its separate fake key |
| Text streaming | Created/completed lifecycle, ordered sequence numbers, output-item identities, content deltas, final text, and usage including cached/reasoning tokens |
| Reasoning | Summary events and final reasoning item stay separate from the answer |
| Tool calls | Split argument deltas reconstruct valid JSON; explicit tool results retain call identity on the next request |
| Parallel tools | Two function calls retain separate arguments and call IDs |
| Structured output and images | JSON Schema and strict flag reach upstream; an inline image URL and detail survive translation |
| Concurrency | Four streams retain their own response IDs, text and usage |
| Early failover | A primary upstream 503 switches to a secondary before any client-visible error or duplicate lifecycle |
| Cancellation | Closing a client stream closes upstream; a later request still completes |
| Interrupted upstream | Truncated upstream output ends with an observable error rather than a successful completion |
| Downstream WebSocket | RFC 6455 handshake, client-key rejection, tool call and incremental tool result with `previous_response_id` on the same connection |

A backend without the downstream `/v1/responses` WebSocket route reports that
check as skipped. Other handshake failures, missing events or contract failures
fail the suite. Missing `OMAPROXY_TEST_BINARY` skips the whole protocol lane.

## Run real Codex CLI and app-server checks

The optional lane needs an explicit Codex executable. Use the actual binary
rather than a version-manager wrapper that reads the user's home directory:

```bash
OMAPROXY_TEST_BINARY=/absolute/path/to/cli-proxy-api \
OMAPROXY_TEST_CODEX_BINARY=/absolute/path/to/codex \
  python3 -m unittest discover -s tests -p test_codex_clients.py -v
```

These three checks run:

1. `codex exec` with an ephemeral session and read-only sandbox, and assert its
   completed turn and final agent message from the fake upstream.
2. `codex app-server` over stdio, initialize it, create an ephemeral thread,
   start a turn, and assert streamed message deltas, the completed item and
   successful terminal turn status.
3. Interrupt an active app-server turn, assert the interrupted status and
   upstream cancellation, then complete a new turn on that thread.

Each test uses a temporary `CODEX_HOME`, `HOME`, workspace and XDG directories.
The child environment is allowlisted and contains only a fake API key. Its
custom Responses provider points to the temporary proxy, requires no OpenAI
sign-in, disables inference WebSocket transport, and disables request/stream
retries, analytics and feedback. The fixture returns text and never asks the
client to execute a command. Tests have bounded client and subprocess waits.
They do not read the user's Codex configuration or alter the user's transcripts.

The app-server initialization and turn sequence follows the
[official Codex app-server protocol](https://learn.chatgpt.com/docs/app-server).
The custom provider uses the documented
[Responses provider configuration](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server#using-codex-app-server),
with a fake local key rather than an OAuth access token.

This gives direct Codex CLI acceptance and app-server wire acceptance useful
for clients such as T3. It does not launch T3 or validate its UI, provider
settings, history persistence or reconnection behavior. Missing either binary
environment variable skips this optional lane.

## Coverage limits

The upstream fixtures are synthetic. Schema/image tests prove translation,
not model adherence to the schema or image interpretation. Tool tests prove
protocol identity and argument handling, not the execution of real filesystem
tools. The real Codex lane uses HTTP/SSE inference; the separate WebSocket test
covers the proxy's downstream transport with a local HTTP/SSE upstream.

These lanes do not verify native Codex OAuth, provider quotas, a real provider's
upstream WebSocket implementation, live model access, paid inference or a T3
application session. An all-green run means the listed local contracts passed
for those executable versions. It is not a claim that every provider or client
feature is supported.

## Verified baseline

The `Coding client compatibility` workflow runs these lanes on both reviewed
amd64 releases with Codex 0.160.0. Its archive SHA-256 pins are checked before
extraction. Omarchy manifest and native QML acceptance remain local desktop
checks because the hosted runner does not contain Omarchy.

On 2026-10-03, the 11 protocol checks and three real-client checks passed with
Codex CLI **0.160.0** against each of:

| CLIProxyAPI | Commit | Result |
| --- | --- | --- |
| 7.2.154 | `ba7e5583` | 14 checks passed, none skipped |
| 8.0.13 | `d7914afd` | 14 checks passed, none skipped |

The v8 Linux amd64 release archive was verified against the reviewed SHA-256
`50ecffb47fdd81c8c5a9825a73a7a905ab66342337e274f39c4276b92d3533f3`
before running its binary. These checks do not require upgrading the installed
service. To run the existing unit and backend checks together with these lanes:

```bash
OMAPROXY_TEST_BINARY=/absolute/path/to/cli-proxy-api \
OMAPROXY_TEST_CODEX_BINARY=/absolute/path/to/codex \
  python3 -m unittest discover -s tests -v
node tests/limit-model.test.cjs
omarchy plugin validate .
```
