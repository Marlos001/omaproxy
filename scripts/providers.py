"""Custom OpenAI-compatible providers. Caller serializes read/modify/write operations.

Credentials stay in this module and travel in management request bodies only.
The request callable used for explicit tests must refuse redirects and proxies.
"""
import copy
import re
import urllib.error
import urllib.parse

ROUTE = "/v0/management/openai-compatibility"
MAX_WEIGHT = 1_000_000


def validate_url(value):
    if not isinstance(value, str) or any(ord(c) < 33 for c in value):
        raise ValueError("Enter a base URL without whitespace or control characters.")
    parsed = urllib.parse.urlsplit(value)
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("Enter a valid endpoint port.") from error
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("Use an HTTPS endpoint, or an HTTP endpoint on localhost.")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or "\\" in value:
        raise ValueError("Enter a base URL without credentials, query parameters, or fragments.")
    return value.rstrip("/")


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", value):
        raise ValueError("Use a provider name with letters, numbers, underscores, or hyphens.")
    return value


def _entries(api):
    response = api(ROUTE)
    entries = response.get("openai-compatibility") if isinstance(response, dict) else None
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("The backend returned an unsupported provider list.")
    return entries


def _target(entries, name):
    matches = [entry for entry in entries if entry.get("name") == name]
    if len(matches) > 1:
        raise ValueError("The backend has duplicate provider names. Resolve them before editing.")
    return matches[0] if matches else None


def _display_text(value, limit=256):
    return value[:limit] if isinstance(value, str) and not any(ord(c) < 32 for c in value) else ""


def _public(entry):
    # Base URLs may contain credentials in configurations created elsewhere.
    try:
        url = validate_url(entry.get("base-url", ""))
    except ValueError:
        url = ""
    keys = entry.get("api-key-entries", [])
    keys = keys if isinstance(keys, list) else []
    models = entry.get("models", [])
    models = models if isinstance(models, list) else []
    return {"name": _display_text(entry.get("name"), 60), "url": url,
            "models": [{"name": _display_text(model.get("name")),
                        "alias": _display_text(model.get("alias"))}
                       for model in models if isinstance(model, dict)],
            "disabled": entry.get("disabled") is True,
            "credential_count": len(keys),
            "credentials": [{"index": index,
                             "has_key": bool(key.get("api-key")),
                             "weight": key.get("weight") if type(key.get("weight")) is int else None}
                            for index, key in enumerate(keys) if isinstance(key, dict)]}


def list_providers(api):
    return {"custom_providers": [_public(entry) for entry in _entries(api)]}


def _models(value, previous):
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list) or not 1 <= len(value) <= 500:
        raise ValueError("Enter at least one model ID (at most 500).")
    result, aliases = [], set()
    for item in value:
        if isinstance(item, str):
            name, alias = item, item
        elif isinstance(item, dict):
            name, alias = item.get("name"), item.get("alias") or item.get("name")
        else:
            raise ValueError("Each model needs a name and optional alias.")
        for text in (name, alias):
            if not isinstance(text, str) or not text or len(text) > 256 or any(ord(c) < 33 for c in text):
                raise ValueError("Use model IDs without whitespace or control characters.")
        if alias in aliases:
            raise ValueError("Use a distinct alias for each model.")
        aliases.add(alias)
        old = next((model for model in previous if isinstance(model, dict) and
                    model.get("name") == name and (model.get("alias") or name) == alias), {})
        model = copy.deepcopy(old)
        model.update(name=name, alias=alias)
        result.append(model)
    return result


def upsert_provider(api, payload, *, weights_supported=False, create_only=False):
    if not isinstance(payload, dict):
        raise ValueError("Provide a provider object.")
    if set(payload) - {"name", "url", "key", "models", "weight", "credential_index", "disabled"}:
        raise ValueError("Unsupported provider field.")
    name = _name(payload.get("name"))
    entries = _entries(api)
    old = _target(entries, name)
    if old and create_only:
        raise ValueError("That provider already exists. Choose a different name.")
    patch = {}
    if "url" in payload:
        patch["base-url"] = validate_url(payload["url"])
    elif not old:
        raise ValueError("Enter a provider base URL.")
    if "models" in payload:
        patch["models"] = _models(payload["models"], old.get("models", []) if old else [])
    elif not old:
        raise ValueError("Enter at least one model ID.")
    if "disabled" in payload:
        if type(payload["disabled"]) is not bool:
            raise ValueError("disabled must be a boolean.")
        patch["disabled"] = payload["disabled"]
    key = payload.get("key", "")
    if not isinstance(key, str) or any(ord(c) < 32 for c in key):
        raise ValueError("Enter a valid API key.")
    key = key.strip()
    if "weight" in payload:
        weight = payload["weight"]
        if not weights_supported:
            raise ValueError("Credential weights are unavailable for this backend version.")
        if weight is not None and (type(weight) is not int or not 0 <= weight <= MAX_WEIGHT):
            raise ValueError(f"Credential weight must be between 0 and {MAX_WEIGHT}, or null to reset.")
    if key or "weight" in payload:
        keys = copy.deepcopy(old.get("api-key-entries", []) if old else [])
        index = payload.get("credential_index", 0)
        if type(index) is not int or index < 0:
            raise ValueError("Choose a valid credential index.")
        if len(keys) > 1 and "credential_index" not in payload:
            raise ValueError("Choose which provider credential to edit.")
        if not keys and index == 0:
            keys.append({})
        if index >= len(keys):
            raise ValueError("That provider credential does not exist.")
        if key:
            keys[index]["api-key"] = key
        if "weight" in payload:
            if payload["weight"] is None:
                keys[index].pop("weight", None)
            else:
                keys[index]["weight"] = payload["weight"]
        # auth_index is a backend display identifier, never stored config.
        for credential in keys:
            credential.pop("auth_index", None)
            credential.pop("auth-index", None)
        patch["api-key-entries"] = keys
    elif not old:
        patch["api-key-entries"] = []
    effective_url = validate_url(patch.get("base-url", old.get("base-url", "") if old else ""))
    effective_keys = patch.get("api-key-entries", old.get("api-key-entries", []) if old else [])
    if not isinstance(effective_keys, list) or any(not isinstance(item, dict) for item in effective_keys):
        raise ValueError("The backend returned an unsupported provider credential list.")
    has_key = any(isinstance(item.get("api-key"), str) and item["api-key"].strip() for item in effective_keys)
    if urllib.parse.urlsplit(effective_url).hostname not in ("localhost", "127.0.0.1", "::1") and not has_key:
        raise ValueError("Enter an API key for a remote provider.")
    if old:
        if not patch:
            raise ValueError("Provide at least one provider change.")
        # Omission of api-key-entries preserves ALL credentials when key is blank.
        api(ROUTE, "PATCH", {"name": name, "value": patch})
    else:
        new = {"name": name, **patch}
        # v0 has no append operation. Never replace unrelated definitions when
        # editing/removing; only creation needs a locked array read + PUT.
        api(ROUTE, "PUT", copy.deepcopy(entries) + [new])
    confirmed = _target(_entries(api), name)
    if not confirmed:
        raise ValueError("Backend did not confirm the provider. Refresh before retrying.")
    for field in ("base-url", "models", "disabled"):
        if field in patch and confirmed.get(field, False if field == "disabled" else None) != patch[field]:
            raise ValueError("Backend did not confirm all provider changes. Refresh before retrying.")
    if "api-key-entries" in patch:
        confirmed_keys = copy.deepcopy(confirmed.get("api-key-entries", []))
        for credential in confirmed_keys:
            credential.pop("auth_index", None)
            credential.pop("auth-index", None)
        if confirmed_keys != patch["api-key-entries"]:
            raise ValueError("Backend did not confirm the credential changes. Refresh before retrying.")
    return {"message": "API provider updated." if old else "API provider added. Its models are now available.",
            "custom_provider": _public(confirmed)}


def remove_provider(api, name):
    name = _name(name)
    if not _target(_entries(api), name):
        raise ValueError("That provider does not exist.")
    api(ROUTE + "?" + urllib.parse.urlencode({"name": name}), "DELETE")
    if _target(_entries(api), name):
        raise ValueError("Backend did not confirm provider removal. Refresh before retrying.")
    return {"message": "API provider removed.", "custom_providers": list_providers(api)["custom_providers"]}


def test_provider(api, name, request_fn, *, credential_index=0):
    """Explicit GET /models only. Never infer a model or make a completion call."""
    name = _name(name)
    entry = _target(_entries(api), name)
    if entry is None:
        raise ValueError("That provider does not exist.")
    url = validate_url(entry.get("base-url", ""))
    # Arbitrary configured authorization/header overrides are intentionally not
    # copied to the request; those providers require a backend-specific check.
    if entry.get("headers"):
        raise ValueError("Provider tests with custom headers are unsupported.")
    keys = entry.get("api-key-entries", [])
    if type(credential_index) is not int or credential_index < 0 or (keys and credential_index >= len(keys)) or (not keys and credential_index != 0):
        raise ValueError("Choose a valid credential index.")
    key = keys[credential_index].get("api-key") if keys else None
    try:
        result = request_fn(url + "/models", key=key, method="GET", timeout=8)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        raise ValueError(f"Provider model discovery returned HTTP {code}.") from None
    except Exception:
        raise ValueError("Provider model discovery failed.") from None
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise ValueError("Provider returned an unsupported model list.")
    ids = [_display_text(model.get("id")) for model in result["data"] if isinstance(model, dict)]
    return {"message": "Provider model discovery succeeded. Inference was not tested.",
            "provider_test": {"name": name, "models": [value for value in ids if value], "inference_tested": False}}
