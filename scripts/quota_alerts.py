"""Private, opt-in desktop alerts for fresh OmaProxy quota snapshots."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


MAX_DATA_AGE_SECONDS = 10 * 60
FUTURE_SKEW_SECONDS = 30
MAX_STATE_ENTRIES = 200
MAX_STATE_BYTES = 256 * 1024
STATE_RETENTION_SECONDS = 90 * 24 * 60 * 60
LOW_REMAINING_PERCENT = 10

_PROVIDERS = {
    "antigravity": "Antigravity",
    "claude": "Claude",
    "codex": "Codex",
    "gemini": "Gemini",
    "gemini-cli": "Gemini",
    "github-copilot": "GitHub Copilot",
    "kimi": "Kimi",
    "qwen": "Qwen",
    "xai": "xAI",
}
_PROVIDER_LABELS = set(_PROVIDERS.values()) | {"Provider"}
_AUTH_FAILURE_STATUSES = {"auth_error", "authentication_error", "expired", "unauthorized"}
_HEALTHY_STATUSES = {"active", "authenticated", "healthy", "ok", "ready", "success", "valid"}
_ENV_ALLOWLIST = ("DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "LANG", "LC_ALL", "LC_MESSAGES",
                  "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_DURATION_LABEL = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:-|\s)?(hour|hours|minute|minutes)$", re.I)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _is_fresh(value, now):
    stamp = _number(value)
    return (stamp is not None and stamp > 0 and stamp <= now + FUTURE_SKEW_SECONDS
            and now - stamp <= MAX_DATA_AGE_SECONDS)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _account_identity(account):
    value = account.get("auth_index") or account.get("name")
    if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
        return _digest(str(value))
    return None


def _provider_label(account):
    raw = account.get("provider") or account.get("type")
    if not isinstance(raw, str):
        return "Provider"
    return _PROVIDERS.get(raw.strip().casefold(), "Provider")


def _window_label(value):
    """Map known quota labels to generic standard labels; never echo provider text."""
    if not isinstance(value, str) or len(value) > 160:
        return None
    candidate = value.strip().rsplit("·", 1)[-1].strip()
    key = candidate.casefold().replace("_", " ").strip()
    aliases = {
        "weekly": "Weekly", "7 day": "Weekly", "7-day": "Weekly", "7 days": "Weekly",
        "monthly": "Monthly", "primary window": "Primary window",
        "secondary window": "Secondary window", "quota": "Quota",
    }
    if key in aliases:
        return aliases[key]
    match = _DURATION_LABEL.fullmatch(key)
    if match:
        value = float(match.group(1))
        if math.isfinite(value) and value > 0 and value <= 10000:
            amount = f"{value:g}"
            unit = "hour" if match.group(2).casefold().startswith("hour") else "minute"
            return f"{amount}-{unit}"
    return None


def _status(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\s-]+", "_", value.strip().casefold())


def _event_id(account_digest, kind, window, generation):
    return _digest("\0".join((account_digest, kind, window, str(generation))))


def _observation_id(account_digest, window):
    return _digest("\0".join((account_digest, "observation", window)))


def _window_id(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        return None
    # Keep otherwise-identically-labeled provider windows distinct without
    # retaining or exposing their raw provider-supplied names.
    return _digest(" ".join(value.casefold().split()))


def _state_paths(config_path):
    directory = Path(config_path).expanduser()
    # A mistaken caller must not leave persistent alert state in a checkout.
    repository = Path(__file__).resolve().parents[1]
    try:
        directory.resolve().relative_to(repository)
    except ValueError:
        pass
    else:
        raise ValueError("Alert state must be stored outside the repository.")
    return directory / "quota-alerts.json", directory / "quota-alerts.lock"


def _clean_entry(entry):
    if not isinstance(entry, dict):
        return None
    kind = entry.get("kind")
    if kind not in {"observation", "low", "reset", "authfailure"}:
        return None
    account_digest = entry.get("account")
    if not isinstance(account_digest, str) or not _HEX_DIGEST.fullmatch(account_digest):
        return None
    touched = _number(entry.get("touched"))
    if touched is None or touched <= 0:
        return None
    result = {"kind": kind, "account": account_digest, "touched": touched}
    if kind == "observation":
        window = _window_label(entry.get("window"))
        window_id = entry.get("window_id")
        remaining = _number(entry.get("remaining"))
        generation = entry.get("generation")
        if (window is None or not isinstance(window_id, str) or not _HEX_DIGEST.fullmatch(window_id)
                or remaining is None or not 0 <= remaining <= 100):
            return None
        if not isinstance(generation, str) or len(generation) > 32:
            return None
        reset_at = _number(entry.get("reset_at"))
        result.update(window=window, window_id=window_id, remaining=remaining, generation=generation,
                      reset_at=reset_at)
        return result

    delivery = entry.get("delivery")
    if delivery not in {"pending", "sent"}:
        return None
    provider = entry.get("provider")
    if provider not in _PROVIDER_LABELS:
        return None
    result.update(delivery=delivery, provider=provider,
                  created=_number(entry.get("created")) or touched)
    if kind != "authfailure":
        window = _window_label(entry.get("window"))
        window_id = entry.get("window_id")
        if window is None or not isinstance(window_id, str) or not _HEX_DIGEST.fullmatch(window_id):
            return None
        result.update(window=window, window_id=window_id)
        generation = entry.get("generation")
        if not isinstance(generation, str) or len(generation) > 32:
            return None
        result["generation"] = generation
    if kind == "low":
        remaining = _number(entry.get("remaining"))
        if remaining is None or not 0 <= remaining <= LOW_REMAINING_PERCENT:
            return None
        result["remaining"] = remaining
    return result


def _load_state(path):
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            return {"entries": {}, "unsupported_reported": False}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"entries": {}, "unsupported_reported": False}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"entries": {}, "unsupported_reported": False}
    entries = {}
    for key, raw_entry in data["entries"].items():
        if not isinstance(key, str) or not _HEX_DIGEST.fullmatch(key):
            continue
        entry = _clean_entry(raw_entry)
        if entry is not None:
            entries[key] = entry
    return {"entries": entries,
            "unsupported_reported": data.get("unsupported_reported") is True}


def _trim_state(state, now):
    entries = state["entries"]
    for key in list(entries):
        touched = _number(entries[key].get("touched"))
        if touched is None or now - touched > STATE_RETENTION_SECONDS:
            del entries[key]
    if len(entries) <= MAX_STATE_ENTRIES:
        return

    def priority(item):
        entry = item[1]
        if entry["kind"] == "authfailure" and entry.get("delivery") == "pending":
            rank = 0
        elif entry["kind"] == "authfailure":
            rank = 1
        elif entry.get("delivery") == "pending":
            rank = 2
        elif entry.get("delivery") == "sent":
            rank = 3
        else:
            rank = 4
        return rank, -entry.get("touched", 0)

    kept = sorted(entries.items(), key=priority)[:MAX_STATE_ENTRIES]
    state["entries"] = dict(kept)


def _write_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".quota-alerts-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, **state}, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.fchmod(fd, 0o600)
    lock = os.fdopen(fd, "w")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    return lock


def _queue_event(entries, account_digest, kind, window, window_id, generation,
                 provider, now, remaining=None):
    key = _event_id(account_digest, kind, window_id, generation)
    existing = entries.get(key)
    if existing is not None:
        if existing.get("delivery") == "pending":
            existing["provider"] = provider
            existing["touched"] = now
            if kind == "low":
                existing["remaining"] = remaining
        return key
    entry = {"kind": kind, "delivery": "pending", "account": account_digest,
             "provider": provider, "created": now, "touched": now}
    if kind != "authfailure":
        entry.update(window=window, window_id=window_id, generation=str(generation))
    if kind == "low":
        entry["remaining"] = remaining
    entries[key] = entry
    return key


def _command(entry, executable):
    if entry["kind"] == "low":
        title = "OmaProxy quota alert"
        body = f"{entry['provider']} · {entry['window']}: {entry['remaining']:g}% remaining."
    elif entry["kind"] == "reset":
        title = "OmaProxy quota refreshed"
        body = f"{entry['provider']} · {entry['window']} allowance refreshed."
    else:
        title = "OmaProxy sign-in needed"
        body = f"{entry['provider']} account needs sign-in."
    return [executable, "--app-name=OmaProxy", "--", title, body]


def _desktop_environment():
    return {key: os.environ[key] for key in _ENV_ALLOWLIST if os.environ.get(key)}


def _send(runner, argv):
    try:
        result = runner(argv, check=False, timeout=5, shell=False,
                        env=_desktop_environment(), stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False
    return getattr(result, "returncode", None) == 0


def process(snapshot, config_path, now=None, runner=None):
    """Send de-duplicated alerts for a fresh normalized quota snapshot.

    `config_path` is the private configuration directory (for example
    ``~/.config/omaproxy``). `runner`, when supplied, has the same call contract
    as ``subprocess.run`` and is intended for deterministic tests.
    """
    current = time.time() if now is None else _number(now)
    if current is None or not isinstance(snapshot, dict):
        return {"alert_count": 0, "error": "Invalid quota alert input."}
    if not _is_fresh(snapshot.get("checked_at"), current):
        return {"alert_count": 0}
    accounts = snapshot.get("accounts")
    if not isinstance(accounts, list):
        return {"alert_count": 0}
    try:
        state_path, lock_path = _state_paths(config_path)
    except (OSError, TypeError, ValueError):
        return {"alert_count": 0, "error": "Private alert state path is unavailable."}

    sent_count = 0
    delivery_failed = False
    unsupported_error = ""
    lock = None
    try:
        lock = _lock(lock_path)
        state = _load_state(state_path)
        entries = state["entries"]
        eligible = set()

        for account in accounts:
            if not isinstance(account, dict):
                continue
            account_digest = _account_identity(account)
            if account_digest is None or not _is_fresh(account.get("checked_at"), current):
                continue
            disabled = account.get("disabled") is True
            provider = _provider_label(account)
            status = _status(account.get("status"))
            auth_id = _event_id(account_digest, "authfailure", "", "current")

            if disabled:
                entries.pop(auth_id, None)
            elif status in _AUTH_FAILURE_STATUSES:
                _queue_event(entries, account_digest, "authfailure", "Account", "", "current",
                             provider, current)
                eligible.add(auth_id)
            elif status in _HEALTHY_STATUSES:
                entries.pop(auth_id, None)

            if disabled or account.get("stale") or account.get("error"):
                continue
            if not _is_fresh(account.get("updated_at"), current):
                continue
            windows = account.get("windows")
            if not isinstance(windows, list):
                continue

            for window_data in windows:
                if not isinstance(window_data, dict):
                    continue
                raw_label = window_data.get("label")
                label = _window_label(raw_label)
                window_id = _window_id(raw_label)
                remaining = _number(window_data.get("remaining_percent"))
                if label is None or window_id is None or remaining is None or not 0 <= remaining <= 100:
                    continue
                reset_at = _number(window_data.get("reset_at"))
                generation = "unknown" if reset_at is None else str(int(reset_at // 60))
                observation_key = _observation_id(account_digest, window_id)
                previous = entries.get(observation_key)

                if (previous and previous.get("kind") == "observation"
                        and previous.get("generation") != generation
                        and previous.get("reset_at") is not None
                        and reset_at is not None and reset_at > current - FUTURE_SKEW_SECONDS
                        and remaining > previous.get("remaining", 100)):
                    reset_id = _queue_event(entries, account_digest, "reset", label, window_id,
                                            generation, provider, current)
                    if remaining > 0:
                        eligible.add(reset_id)

                if previous and previous.get("generation") != generation:
                    for pending_id, pending_event in list(entries.items()):
                        if (pending_event.get("account") == account_digest
                                and pending_event.get("window_id") == window_id
                                and pending_event.get("generation") != generation
                                and pending_event.get("delivery") == "pending"):
                            del entries[pending_id]

                entries[observation_key] = {
                    "kind": "observation", "account": account_digest, "window": label,
                    "window_id": window_id,
                    "generation": generation, "remaining": remaining,
                    "reset_at": reset_at, "touched": current,
                }

                if remaining <= LOW_REMAINING_PERCENT:
                    low_id = _queue_event(entries, account_digest, "low", label, window_id,
                                          generation, provider, current, remaining)
                    eligible.add(low_id)
                else:
                    low_id = _event_id(account_digest, "low", window_id, generation)
                    pending = entries.get(low_id)
                    if pending is not None and pending.get("delivery") == "pending":
                        del entries[low_id]

                # A failed notify-send remains retryable while the fresh backend
                # reading still supports the same reset generation.
                reset_id = _event_id(account_digest, "reset", window_id, generation)
                reset_pending = entries.get(reset_id)
                if (reset_pending is not None and reset_pending.get("delivery") == "pending"
                        and remaining > 0):
                    eligible.add(reset_id)

        # Do not deliver persisted pending notifications from accounts/windows
        # that this fresh snapshot did not validate.
        pending_ids = [key for key in entries if key in eligible
                       and entries[key].get("delivery") == "pending"]
        executable = shutil.which("notify-send") if pending_ids else None
        if pending_ids and executable is None:
            if not state["unsupported_reported"]:
                unsupported_error = "Desktop notifications are unavailable (notify-send not found)."
                state["unsupported_reported"] = True
        elif executable is not None:
            command_runner = runner or subprocess.run
            for key in pending_ids:
                entry = entries[key]
                if _send(command_runner, _command(entry, executable)):
                    entry["delivery"] = "sent"
                    entry["touched"] = current
                    sent_count += 1
                else:
                    entry["touched"] = current
                    delivery_failed = True

        _trim_state(state, current)
        _write_state(state_path, state)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.SubprocessError):
        return {"alert_count": sent_count,
                "error": "Private alert state could not be read or written."}
    finally:
        if lock is not None:
            lock.close()

    result = {"alert_count": sent_count}
    if unsupported_error:
        result["error"] = unsupported_error
    elif delivery_failed:
        result["error"] = "One or more desktop notifications could not be delivered."
    return result
