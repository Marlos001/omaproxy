# Named client access keys

Use a separate downstream access key for each client, for example `codex-cli`, `t3-code`, `opencode`, `agy` or `kiro-cli`. Creating a named key enables it on the proxy. Copy it to the intended client's credential configuration. Creating a key does not change that client's configuration automatically.

Client names contain 1-60 ASCII letters, digits, dots, underscores or hyphens and start with a letter or digit. Names identify the keys the user created here. Existing backend keys remain unmanaged; the helper does not assign names to the primary key or other existing credentials.

## Storage and display

The private `client-keys.json` registry contains only a name, a keyed HMAC label and a creation state for each managed key. It never stores raw access keys. Writes use an atomic replacement with mode `0600`; newly created registry directories use mode `0700`. Invalid, oversized or symlinked registry files fail without replacement.

`key_label` is `client-` followed by the first 16 hex characters of HMAC-SHA256 over `client\0` plus the access-key value. The private management key is the HMAC salt. This matches diagnostics' client labels when diagnostics uses the same salt. The salt must be stable and nonempty. Changing the management key changes these labels; old registry entries cannot then be matched automatically.

Lists return only `{name, key_label, active}` rows. `active` describes whether the exact managed key appears in the current backend access-key list. An unavailable or invalid backend list raises a fixed error; it does not turn missing evidence into inactive rows. Both registry and backend lists are limited to 200 entries, and the registry is limited to 64 KiB. An oversized backend list blocks mutations rather than truncating the population.

Copy fetches the access-key list from the backend, matches the HMAC label, and sends the selected raw key only to `wl-copy` standard input. The token is absent from command arguments and returned JSON. Clipboard output and error streams are discarded. Errors use fixed messages without exception bodies. The clipboard contains the copied credential until the user replaces or clears it.

## Backend contract and recovery

The verified v7 and v8 sources retain the downstream `/v0/management/api-keys` contract:

| Operation | Request |
| --- | --- |
| Read | `GET /v0/management/api-keys`, response `{"api-keys": [...]}` |
| Create | `PATCH /v0/management/api-keys` with `{"old": generated_value, "new": generated_value}` |
| Revoke | `DELETE /v0/management/api-keys?value=encoded_value` |

For `PATCH`, matching `old` replaces that exact entry; an absent `old` appends `new`. Setting both to the same generated key makes a repeated append idempotent. The helper never submits a full replacement array or deletes by index.

Before creation, the helper writes a pending HMAC record. It marks the record active only after backend readback finds the key. A failed response or readback retains that record. Repeating creation with the same name reads the existing key and does not generate another credential. If the named key remains absent, creation fails until the user refreshes or revokes the inactive record. Revocation of an inactive record removes only its local name.

The backend's delete-by-value operation compares trimmed values. Revocation refuses a primary-key match or any distinct key with the same trimmed value. Callers must supply the real `settings.api_key` as `primary_key` to revoke; omission fails before contacting the backend. Readback must confirm absence before removing the name. A failed revocation retains the record for retry. Other backend keys remain untouched.

The caller must hold its management mutation lock across creation and revocation, including registry writes and readback. This protects local named operations; it does not lock changes made independently in another management client. The helper does not read private settings itself.

## Client attribution

A separate key supports attribution only when a request receipt actually includes the downstream client access-key value or an explicitly documented downstream client-key source field. Match that value with the same HMAC salt, then resolve its named record.

Upstream provider API-key counters, OAuth account counters, model names and routing choices do not identify a downstream client. A named key list proves access configuration, not that a particular request came from the named client. The current upstream aggregate diagnostics do not supply that request attribution.

## Helper API

```python
list_keys(api, path, salt, primary_key=None)
create_key(api, path, salt, name, primary_key=None)
revoke_key(api, path, salt, name, primary_key=None)
copy_key(api, path, salt, name, primary_key=None, runner=subprocess.run)
```

`path` is the complete registry filename. `api` accepts absolute management routes with `method="GET"`, `body=None` and optional `timeout=4`; it raises on request failures. Creation returns `client_keys` and the sanitized selected `client_key`. Revocation returns `client_keys`, `revoked` and `name`. Copy returns `copied`, `name` and `key_label`. No result contains the raw key.

Run `python -m unittest discover -s tests -p test_client_keys.py -v`. Set `OMAPROXY_TEST_BINARY` to a vetted backend executable to enable the isolated integration test. It starts a temporary loopback backend, creates and revokes one key, and verifies primary-key, upstream-key and YAML-comment preservation. It neither reads nor modifies live settings or the live service.
