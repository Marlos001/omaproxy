"""Bounded, read-only CLIProxyAPI metadata. Raw responses never cross this boundary."""

import hashlib
import hmac
import os
import re
from datetime import datetime
from urllib.error import HTTPError

MAX_RECORDS = 128
MAX_BUCKETS = 20
MAX_EVENTS = 50
TIMEOUT = 2
_SESSION_SALT = os.urandom(32)
_PROVIDERS = frozenset(("codex", "claude", "gemini", "qwen", "kimi", "antigravity",
                        "github-copilot", "openai", "openai-compatible", "unknown"))
_TOKENS = ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens",
           "cache_read_tokens", "cache_creation_tokens", "total_tokens")
_USAGE_PATHS = ("/v8/management/observability/usage/api-keys", "/v0/management/api-key-usage")
_ACCOUNT_PATHS = ("/v8/management/credentials", "/v0/management/auth-files")
_QUEUE_PATHS = ("/v8/management/observability/usage/queue?count=50", "/v0/management/usage-queue?count=50")
_QUEUE_NOTE = "Queue reads remove records from the backend. Automatic diagnostics do not consume the queue."
_LIMITATIONS = [
    "Upstream API-key counters exclude OAuth accounts and do not identify client access keys.",
    "Account counters describe backend attempts, not unique user requests or billing totals.",
    "Counters and recent buckets are backend memory snapshots; restarts can reset them.",
    "Client identity, request routing, models, retries, latency, TTFT and tokens are unavailable from these aggregate counters.",
    "Recent buckets use backend local time and contain no date or timezone.",
]


def _salt(value):
    if isinstance(value, str) and value:
        return value.encode()
    if isinstance(value, bytes) and value:
        return value
    return _SESSION_SALT


def _label(kind, value, salt):
    if not isinstance(value, str) or not value or len(value) > 16384:
        return None
    digest = hmac.new(salt, (kind + "\0" + value).encode(), hashlib.sha256).hexdigest()[:16]
    return kind + "-" + digest


def _provider(value, salt):
    return value if isinstance(value, str) and value in _PROVIDERS else _label("provider", value, salt) or "unknown"


def _number(value):
    # JSON booleans, numeric strings, negatives and nonfinite values are not counts.
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _counter(record):
    if not isinstance(record, dict):
        return None
    success, failed = _number(record.get("success")), _number(record.get("failed"))
    if success is None or failed is None or not isinstance(record.get("recent_requests"), list):
        return None
    buckets = []
    for bucket in record["recent_requests"][-MAX_BUCKETS:]:
        if not isinstance(bucket, dict):
            continue
        good, bad = _number(bucket.get("success")), _number(bucket.get("failed"))
        time = bucket.get("time")
        if good is None or bad is None:
            continue
        # Only backend clock labels, never arbitrary text from a bucket.
        if not isinstance(time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", time):
            time = "unknown"
        buckets.append({"time": time, "success": good, "failed": bad})
    return {"success": success, "failed": failed, "recent_requests": buckets}


def _section(availability="unknown", source="", error=""):
    return {"availability": availability, "source": source, "error": error,
            "records": [], "retained": 0, "omitted": 0, "invalid": 0}


def _fetch(api, paths):
    for path in paths:
        try:
            return api(path, timeout=TIMEOUT), _section("available", path)
        except Exception as exc:
            # Exception bodies and URLs can contain keys, auth filenames or upstream responses.
            code = getattr(exc, "code", None)
            if isinstance(exc, HTTPError):
                exc.close()
            if code in (404, 405, 501):
                continue
            message = "Telemetry request failed."
            if code in (401, 403):
                message = "Telemetry access was denied."
            elif code == 429:
                message = "Telemetry endpoint is rate-limited."
            return None, _section("unavailable", path, message)
    return None, _section("unsupported", error="No supported read-only telemetry endpoint was found.")


def _finish(section, records, invalid, omitted):
    section.update(records=records, retained=len(records), invalid=invalid, omitted=omitted)
    # Invalid records are not proof of zero activity.
    if invalid and not records:
        section.update(availability="unknown", error="Telemetry schema was not recognized.")
    return section


def _usage(payload, section, salt):
    if not isinstance(payload, dict) or "error" in payload:
        return _finish(section, [], 1, 0)
    records, invalid, omitted, inspected = [], 0, 0, 0
    for provider, entries in payload.items():
        if inspected >= MAX_RECORDS:
            omitted += len(entries) if isinstance(entries, dict) else 1
            continue
        if not isinstance(entries, dict):
            invalid += 1
            inspected += 1
            continue
        for composite, raw in entries.items():
            if inspected >= MAX_RECORDS:
                omitted += 1
                continue
            inspected += 1
            counter = _counter(raw)
            identity = _label("upstream-key", str(provider) + "\0" + composite, salt) if isinstance(composite, str) else None
            if counter is None or identity is None:
                invalid += 1
                continue
            records.append(dict(counter, label=identity, provider=_provider(provider, salt)))
    return _finish(section, records, invalid, omitted)


def _accounts(payload, section, salt):
    if not isinstance(payload, list):
        return _finish(section, [], 1, 0)
    records, invalid = [], 0
    for raw in payload[:MAX_RECORDS]:
        counter = _counter(raw)
        if counter is None:
            invalid += 1
            continue
        identity = _label("account", raw.get("auth_index"), salt)
        if identity is None:
            # Auth IDs can be filenames; hashing them is safe but does not establish request attribution.
            identity = _label("account", raw.get("id"), salt)
        if identity is None:
            invalid += 1
            continue
        records.append(dict(counter, label=identity, provider=_provider(raw.get("provider", raw.get("type")), salt)))
    return _finish(section, records, invalid, max(0, len(payload) - MAX_RECORDS))


def snapshot(api, *, accounts=None, salt=None, consume_queue=False):
    """Call GET-only adapter api(full_path, timeout=2), return display-safe metadata.

    Pass raw auth-file records through accounts to reuse an existing backend fetch.
    Pass a private installation salt for stable labels across bridge invocations.
    consume_queue=True is an explicit destructive capture: other queue consumers
    lose these records. No history is persisted. Never enable this for polling.
    No raw payloads, keys, arbitrary text, log files or exception strings are returned.
    """
    salt = _salt(salt)
    payload, usage = _fetch(api, _USAGE_PATHS)
    if usage["availability"] == "available":
        usage = _usage(payload, usage, salt)
    if accounts is None:
        payload, account_section = _fetch(api, _ACCOUNT_PATHS)
        accounts = payload.get("files") if isinstance(payload, dict) else None
    else:
        account_section = _section("available", "existing-account-snapshot")
    if account_section["availability"] == "available":
        account_section = _accounts(accounts, account_section, salt)
    queue = {"availability": "read_only_unavailable", "source": "", "events": [], "error": _QUEUE_NOTE}
    if consume_queue is True:
        items, result = _fetch(api, _QUEUE_PATHS)
        queue.update(availability=result["availability"], source=result["source"], error=result["error"])
        if result["availability"] == "available":
            if isinstance(items, list):
                queue["events"] = sanitize_events(items, salt=salt)
                queue["retained"] = len(queue["events"])
                queue["omitted"] = max(0, len(items) - MAX_EVENTS)
                queue["invalid"] = min(len(items), MAX_EVENTS) - queue["retained"]
                if queue["invalid"] and not queue["retained"]:
                    queue.update(availability="unknown", error="Captured records did not match the supported telemetry schema.")
            else:
                queue.update(availability="unknown", error="Telemetry schema was not recognized.")
        queue["capture_requested"] = True
        queue["consumed"] = result["availability"] == "available"
        queue["warning"] = "Capture removes up to 50 pending records. Other collectors cannot read them afterwards."
    return {"usage": usage, "accounts": account_section, "queue": queue,
            "client_attribution": "receipt_fields_only" if any("client_label" in event for event in queue["events"]) else "unavailable",
            "limitations": list(_LIMITATIONS)}


def sanitize_events(items, *, salt=None):
    """Redact records already supplied by an explicitly authorized queue consumer.

    This function performs no I/O; snapshot calls it only for explicit capture. Unknown fields,
    failure bodies, prompts, tool data, headers and raw identifier values are dropped.
    Model names are anonymous because arbitrary backend model strings can carry secrets.
    """
    if not isinstance(items, list):
        return []
    salt, events = _salt(salt), []
    for raw in items[-MAX_EVENTS:]:
        if not isinstance(raw, dict):
            continue
        event = {}
        for kind, field in (("client", "api_key"), ("account", "auth_index"),
                            ("request", "request_id"), ("execution", "execution_id"), ("model", "model")):
            label = _label(kind, raw.get(field), salt)
            if label:
                event[kind + "_label"] = label
        event["provider"] = _provider(raw.get("provider"), salt)
        timestamp = raw.get("timestamp")
        if isinstance(timestamp, str) and len(timestamp) <= 40:
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    event["timestamp"] = parsed.isoformat()
            except ValueError:
                pass
        if type(raw.get("failed")) is bool:
            event["outcome"] = "failed" if raw["failed"] else "success"
        for field in ("latency_ms", "ttft_ms"):
            value = _number(raw.get(field))
            if value is not None:
                event[field] = value
        tokens = raw.get("tokens")
        if isinstance(tokens, dict):
            event["tokens"] = {field: _number(tokens[field]) for field in _TOKENS
                               if field in tokens and _number(tokens[field]) is not None}
        fail = raw.get("fail")
        if isinstance(fail, dict):
            status = _number(fail.get("status_code"))
            if status is not None and 100 <= status <= 599:
                event["status_code"] = status
        if len(event) > 1 or event["provider"] != "unknown":
            events.append(event)
    return events
