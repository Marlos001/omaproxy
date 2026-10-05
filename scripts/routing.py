"""Allowlisted routing controls; the caller owns its management mutation lock."""
import re
import urllib.error

V0 = "/v0/management/"
V8_ROUTING = "/v8/management/config/routing"
STRATEGIES = ("round-robin", "weighted-round-robin", "fill-first")
SCALARS = ("request-retry", "max-retry-credentials", "max-retry-interval")
AFFINITY = ("session-affinity", "session-affinity-ttl", "session-affinity-subagents")
COOLDOWN = ("disable-cooling", "save-cooldown-status")
BOUNDS = {"request-retry": (0, 10), "max-retry-credentials": (0, 100),
          "max-retry-interval": (0, 300)}


def _optional_get(api, route):
    try:
        return api(route)
    except urllib.error.HTTPError as error:
        if error.code not in (404, 405):
            raise
        error.close()
        return None


def supports_weights(backend_version):
    """Only offer weighted routing for backend versions whose contract we checked."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", str(backend_version or ""))
    return bool(match and tuple(map(int, match.groups())) >= (7, 2, 154))


def read_settings(api, backend_version=None):
    """GET-only capability discovery. Never return the backend config document."""
    values, capabilities = {}, {}
    strategy = _optional_get(api, V0 + "routing/strategy")
    if isinstance(strategy, dict) and isinstance(strategy.get("strategy"), str):
        values["strategy"] = strategy["strategy"]
        capabilities["strategy"] = True
    for field in SCALARS:
        response = _optional_get(api, V0 + field)
        if isinstance(response, dict) and type(response.get(field)) is int:
            values[field] = response[field]
            capabilities[field] = True
    tree = _optional_get(api, V8_ROUTING)
    v8 = isinstance(tree, dict)
    if not v8:
        # A missing subtree is possible on a fresh v8 config. This safe scalar
        # identifies the v8 config API without reading credentials or full YAML.
        v8 = _optional_get(api, "/v8/management/config/config-version") == 8
    if v8:
        tree = tree if isinstance(tree, dict) else {}
        capabilities.update({field: True for field in AFFINITY + COOLDOWN})
        for field in AFFINITY:
            value = tree.get(field)
            if field == "session-affinity-ttl":
                values[field] = value if isinstance(value, str) and value else "1h"
            else:
                values[field] = value if type(value) is bool else field.endswith("subagents")
        cooling = tree.get("cooldown", {})
        if isinstance(cooling, dict):
            for field in COOLDOWN:
                values[field] = cooling.get(field) if type(cooling.get(field)) is bool else False
        retry = tree.get("retry", {})
        if isinstance(retry, dict):
            for field in SCALARS:
                if type(retry.get(field)) is int:
                    values[field] = retry[field]
                    capabilities[field] = True
        if isinstance(tree.get("strategy"), str):
            values["strategy"] = tree["strategy"]
            capabilities["strategy"] = True
    else:
        config = _optional_get(api, V0 + "config")
        if isinstance(config, dict):
            routing = config.get("routing", {})
            if isinstance(routing, dict):
                for field in AFFINITY:
                    value = routing.get(field)
                    if field == "session-affinity-ttl":
                        values[field] = value if isinstance(value, str) and value else "1h"
                    else:
                        values[field] = value if type(value) is bool else field.endswith("subagents")
            for field in COOLDOWN:
                values[field] = config.get(field) if type(config.get(field)) is bool else False
        capabilities.update({field: False for field in AFFINITY + COOLDOWN})
    weighted = supports_weights(backend_version) or values.get("strategy") == "weighted-round-robin"
    options = [item for item in STRATEGIES if weighted or item != "weighted-round-robin"]
    return {"values": values, "capabilities": capabilities, "strategies": options,
            "weights": weighted, "v8_config": v8,
            "limitations": [] if v8 else ["Session affinity and cooldown flags require the v8 config PATCH API to edit safely."]}


def _duration(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:\d+(?:\.\d+)?(?:h|m|s))+", value):
        raise ValueError("Use a positive duration such as 30m, 1h, or 2h30m.")
    seconds = sum(float(number) * {"h": 3600, "m": 60, "s": 1}[unit]
                  for number, unit in re.findall(r"(\d+(?:\.\d+)?)(h|m|s)", value))
    if not 1 <= seconds <= 86400:
        raise ValueError("Session affinity TTL must be between 1 second and 24 hours.")
    return value


def validate_changes(changes):
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Provide at least one routing setting.")
    unknown = set(changes) - {"strategy", *SCALARS, *AFFINITY, *COOLDOWN}
    if unknown:
        raise ValueError("Unsupported routing setting.")
    for field, value in changes.items():
        if field == "strategy":
            if value not in STRATEGIES:
                raise ValueError("Choose a supported routing strategy.")
        elif field in BOUNDS:
            low, high = BOUNDS[field]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{field} must be an integer between {low} and {high}.")
        elif field == "session-affinity-ttl":
            _duration(value)
        elif type(value) is not bool:
            raise ValueError(f"{field} must be a boolean.")
    return dict(changes)


def update_settings(api, changes, backend_version=None):
    """Validate every requested change before writing; no whole-config replacement."""
    changes = validate_changes(changes)
    snapshot = read_settings(api, backend_version)
    for field, value in changes.items():
        if not snapshot["capabilities"].get(field):
            raise ValueError(f"This backend cannot safely edit {field} through its management API.")
        if field == "strategy" and value not in snapshot["strategies"]:
            raise ValueError("Weighted routing is unavailable for this backend version.")
    if snapshot["v8_config"]:
        patch = {}
        for field, value in changes.items():
            if field in SCALARS:
                patch.setdefault("retry", {})[field] = value
            elif field in COOLDOWN:
                patch.setdefault("cooldown", {})[field] = value
            else:
                patch[field] = value
        api(V8_ROUTING, "PATCH", patch)
    else:
        # v0 has only scalar writes. Report any partial failure explicitly;
        # never retry or roll back an unverified backend response.
        applied = []
        for field, value in changes.items():
            try:
                route = "routing/strategy" if field == "strategy" else field
                api(V0 + route, "PATCH", {"value": value})
                applied.append(field)
            except Exception as error:
                raise ValueError("Routing update failed; fields already applied: " +
                                 (", ".join(applied) or "none") + ". Refresh settings before retrying.") from error
    refreshed = read_settings(api, backend_version)
    mismatches = [field for field, value in changes.items() if refreshed["values"].get(field) != value]
    if mismatches:
        raise ValueError("Backend did not confirm all routing changes. Refresh settings before retrying.")
    return {"message": "Routing settings updated.", "routing_settings": refreshed}
