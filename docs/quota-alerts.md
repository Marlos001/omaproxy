# Quota desktop alerts

Quota alerts are optional and stay off unless the user enables them in the
OmaProxy panel. The bridge should call `quota_alerts.process(snapshot,
config_path)` only after it has written a fresh quota snapshot and only while
the preference is enabled. The helper does not fetch provider data or change
the proxy configuration.

The helper uses `notify-send` for local desktop notifications. It reports low
quota at 10% remaining or less, and reports a quota refresh only when two fresh
backend readings show a changed reset time and an increase in available quota.
A timer passing a reset time is not enough. Authentication alerts use only an
explicit account-health `status` of `auth_error`, `authentication_error`,
`expired`, or `unauthorized`; generic `error`/`failed` status values and a quota
lookup's `error` field never trigger an authentication alert.

Alerts are ignored when the snapshot or account reading is more than ten
minutes old, when quota data is stale or has an error, when the account is
disabled, or when the remaining percentage is unknown or invalid. Unrecognized
window labels are not shown. Notification text uses a generic provider label
and a standard window label; it never includes account names, email addresses,
auth filenames, auth indexes, or provider error text.

Sent and pending alert keys use unsalted hashed account and window identifiers.
These are pseudonyms; predictable identifiers can still be guessed. The
bounded state file and lock live in the private OmaProxy config directory:

```text
${XDG_CONFIG_HOME:-~/.config}/omaproxy/quota-alerts.json
${XDG_CONFIG_HOME:-~/.config}/omaproxy/quota-alerts.lock
```

The state file is written atomically with mode `0600` and retains at most 200
entries. Failed notification commands remain pending and can retry on a later
fresh snapshot. If `notify-send` is missing, the first attempted alert returns
an explicit unsupported result, keeps the alert pending, and suppresses repeat
unsupported messages. The runner has a five-second timeout, does not use a
shell, and receives only desktop-session environment variables needed to
contact the notification service.

The processing function returns a sanitized result such as
`{"alert_count": 1}`. It may add a generic `error` when private state cannot be
used, notifications fail, or the desktop notification utility is unavailable.
