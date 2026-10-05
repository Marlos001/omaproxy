"""Named downstream access keys. The caller owns the management mutation lock.

No raw key crosses this module's return boundary or enters the local registry.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
from urllib.parse import urlencode

ROUTE = "/v0/management/api-keys"
MAX_KEYS = 200
MAX_REGISTRY_BYTES = 65536
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,59}\Z")
_LABEL = re.compile(r"client-[a-f0-9]{16}\Z")


def _salt(salt):
    if isinstance(salt, str):
        salt = salt.encode()
    if not isinstance(salt, bytes) or not salt:
        raise ValueError("A private management salt is required.")
    return salt


def _label(key, salt):
    return "client-" + hmac.new(_salt(salt), ("client\0" + key).encode(), hashlib.sha256).hexdigest()[:16]


def _name(name):
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("Client name must use 1-60 letters, digits, dots, underscores or hyphens.")
    return name


def _read(path):
    path = Path(path)
    try:
        if path.is_symlink():
            raise ValueError()
        if not path.exists():
            return []
        if path.stat().st_size > MAX_REGISTRY_BYTES:
            raise ValueError()
        document = json.loads(path.read_text())
        records = document["client_keys"]
        if document.get("version") != 1 or not isinstance(records, list) or len(records) > MAX_KEYS:
            raise ValueError()
        names, labels = set(), set()
        for record in records:
            if not isinstance(record, dict) or set(record) != {"name", "key_label", "state"}:
                raise ValueError()
            _name(record["name"])
            if not isinstance(record["key_label"], str) or not _LABEL.fullmatch(record["key_label"]):
                raise ValueError()
            if record["state"] not in ("pending", "active") or record["name"] in names or record["key_label"] in labels:
                raise ValueError()
            names.add(record["name"])
            labels.add(record["key_label"])
        return records
    except Exception:
        raise ValueError("Client-key registry is unavailable or invalid; no changes were made.") from None


def _write(path, records):
    path = Path(path)
    temporary = None
    try:
        if path.is_symlink():
            raise ValueError()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".client-keys-", dir=path.parent)
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"version": 1, "client_keys": records}, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except Exception:
        raise ValueError("Client-key registry could not be saved; refresh before retrying.") from None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _fetch(api):
    try:
        payload = api(ROUTE)
        keys = payload["api-keys"]
        # Do not silently omit records: mutations require a complete, valid list.
        if "error" in payload or not isinstance(keys, list) or len(keys) > MAX_KEYS:
            raise ValueError()
        if any(not isinstance(key, str) or not key.strip() or len(key) > 16384 for key in keys):
            raise ValueError()
        return keys
    except Exception:
        raise ValueError("Client access-key list is unavailable or incomplete; no success was confirmed.") from None


def _matches(record, keys, salt):
    return [key for key in keys if hmac.compare_digest(record["key_label"], _label(key, salt))]


def _rows(records, keys, salt):
    return [{"name": record["name"], "key_label": record["key_label"],
             "active": bool(_matches(record, keys, salt))} for record in records]


def list_keys(api, path, salt, primary_key=None):
    """Show only explicitly named keys. Unmanaged backend keys remain anonymous."""
    _salt(salt)
    records = _read(path)
    return {"client_keys": _rows(records, _fetch(api), salt)}


def _find(records, name):
    name = _name(name)
    for record in records:
        if record["name"] == name:
            return record
    raise ValueError("Named client key was not found.")


def _selected(records, keys, salt, name):
    rows = _rows(records, keys, salt)
    return {"client_keys": rows, "client_key": next(row for row in rows if row["name"] == name)}


def create_key(api, path, salt, name, primary_key=None):
    """Idempotent by name, including a write whose response/readback was lost."""
    _salt(salt)
    _name(name)
    records, keys = _read(path), _fetch(api)
    existing = next((record for record in records if record["name"] == name), None)
    if existing:
        if not _matches(existing, keys, salt):
            raise ValueError("Named key is inactive or creation is unconfirmed; refresh or revoke its record before recreating.")
        if existing["state"] == "pending":
            existing["state"] = "active"
            _write(path, records)
        return _selected(records, keys, salt, name)
    if len(records) >= MAX_KEYS or len(keys) >= MAX_KEYS:
        raise ValueError("Client-key limit reached.")
    key = "oma-" + secrets.token_urlsafe(32)
    if key in keys or key == primary_key or any(record["key_label"] == _label(key, salt) for record in records):
        raise ValueError("Could not generate a unique client key; retry.")
    record = {"name": name, "key_label": _label(key, salt), "state": "pending"}
    records.append(record)
    # Preserve attribution even if the process exits after the server applies PATCH.
    _write(path, records)
    try:
        # old==new updates an existing exact value or appends if absent.
        api(ROUTE, "PATCH", {"old": key, "new": key})
    except Exception:
        pass  # The response can fail after mutation; the readback decides success.
    keys = _fetch(api)
    if not _matches(record, keys, salt):
        raise ValueError("Client-key creation was not confirmed; its pending record was retained. Refresh before retrying.")
    record["state"] = "active"
    _write(path, records)
    return _selected(records, keys, salt, name)


def revoke_key(api, path, salt, name, primary_key=None):
    """Delete one exact named value. Never delete by index or replace an array."""
    _salt(salt)
    if not isinstance(primary_key, str) or not primary_key.strip():
        raise ValueError("Primary-key protection is required before revoking client keys.")
    records, keys = _read(path), _fetch(api)
    record = _find(records, name)
    matches = _matches(record, keys, salt)
    if matches:
        if len(set(matches)) != 1:
            raise ValueError("Named client key is ambiguous; no changes were made.")
        key = matches[0]
        # DELETE's backend contract compares TrimSpace(value), not exact bytes.
        if (primary_key is not None and key.strip() == str(primary_key).strip()) or any(
                other != key and other.strip() == key.strip() for other in keys):
            raise ValueError("This key is protected or ambiguous; no changes were made.")
        try:
            api(ROUTE + "?" + urlencode({"value": key}), "DELETE")
        except Exception:
            pass
        keys = _fetch(api)
        if _matches(record, keys, salt):
            raise ValueError("Client-key revocation was not confirmed; its record was retained. Refresh before retrying.")
    records.remove(record)
    _write(path, records)
    return {"client_keys": _rows(records, keys, salt), "revoked": True, "name": name}


def copy_key(api, path, salt, name, primary_key=None, runner=subprocess.run):
    """The raw key goes only to wl-copy stdin, never argv, output or registry."""
    _salt(salt)
    records, keys = _read(path), _fetch(api)
    record = _find(records, name)
    matches = _matches(record, keys, salt)
    if len(set(matches)) != 1:
        raise ValueError("Named client key is inactive or ambiguous.")
    try:
        result = runner(["wl-copy"], input=matches[0], text=True, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=4, check=False)
        if result.returncode != 0:
            raise ValueError()
    except Exception:
        raise ValueError("Client key could not be copied.") from None
    return {"copied": True, "name": name, "key_label": record["key_label"]}
