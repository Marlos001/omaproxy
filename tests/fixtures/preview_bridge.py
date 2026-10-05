#!/usr/bin/env python3
"""Offline display fixture. Never imports the real bridge or performs network I/O."""
import json
import os
from pathlib import Path
import sys
import time


def main():
    root = Path(os.environ["OMAPROXY_PREVIEW_STATE"])
    state_file = root / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {
        "running": True, "installed": "v6.9.20", "rollback": False,
        "routing": "round-robin", "autostart": False, "mode": "local", "has_api_key": True,
    }
    args = sys.argv[1:]
    command = args[0] if args else "status"
    payload = json.loads(sys.stdin.readline()) if command in ("routing-save", "custom-save", "custom-add", "connection-save") else None
    with (root / "actions.jsonl").open("a") as stream:
        # Record command names and field names, never supplied values.
        stream.write(json.dumps({"command": command, "argument_count": len(args) - 1, "payload_fields": sorted(payload or {}), "notification_requested": "--notify" in args}) + "\n")
    providers = [{"id": "codex", "name": "OpenAI Codex"}, {"id": "claude", "name": "Claude"}, {"id": "gemini", "name": "Gemini"}]
    accounts = [{"name": f"preview-{p['id']}.json", "auth_index": f"fixture-{p['id']}", "provider": p["id"], "email": f"preview-{p['id']}@example.invalid", "plan": "pro" if p["id"] == "codex" else "max", "disabled": False, "status": "ready", "success": 12, "failed": 1} for p in providers]
    quotas = {"accounts": [{"name": a["name"], "provider": a["provider"], "email": a["email"], "plan": a["plan"], "available": True, "windows": [{"label": "Weekly", "remaining_percent": 72, "reset_at": time.time() + 86400, "used": None, "limit": None}, {"label": "5 hours", "remaining_percent": 43, "reset_at": time.time() + 3600, "used": None, "limit": None}]} for a in accounts]}
    updates = {"installed_version": state["installed"], "latest_version": "v6.9.22", "reviewed_version": "v6.9.22", "update_supported": True, "update_available": state["installed"] != "v6.9.22", "rollback_available": state["rollback"], "error": ""}
    values = {"strategy": state["routing"], "session-affinity": True, "session-affinity-ttl": "1h", "session-affinity-subagents": True, "request-retry": 2, "max-retry-credentials": 2, "max-retry-interval": 30, "disable-cooling": False, "save-cooldown-status": True}
    values.update(state.get("routing_values", {}))
    values["strategy"] = state["routing"]
    routing = {"values": values, "weights": True, "capabilities": {k: True for k in values}, "strategies": ["round-robin", "fill-first", "weighted-round-robin"], "limitations": ["Offline preview: changes are stored only in temporary fixture state."]}
    records = [{"label": "account-preview", "provider": "codex", "success": 12, "failed": 1}]
    diagnostics = {"usage": {"availability": "available", "records": records, "retained": 1, "invalid": 1, "omitted": 2}, "accounts": {"availability": "available", "records": records, "retained": 1, "invalid": 2, "omitted": 3}, "queue": {"availability": "read_only_unavailable", "events": [], "error": "Automatic diagnostics do not consume the queue."}, "client_attribution": "unavailable", "limitations": ["Fixture counters describe backend attempts, not billing totals.", "Client identity and request routing are unavailable from aggregate counters."]}
    custom = state.get("custom", [{"name": "preview-provider", "url": "https://provider.example.invalid/v1", "models": [{"name": "preview-upstream", "alias": "preview-model"}], "credential_count": 2, "weights": [1, 2]}])
    clients = state.get("client_keys", [])
    if command == "status":
        first_status = root / "initial-status-observed"
        if not first_status.exists():
            first_status.touch()
            time.sleep(float(os.environ.get("OMAPROXY_PREVIEW_STATUS_DELAY", "0")))
        mode = state.get("mode", "local")
        remote = mode == "remote"
        result = {"configured": True, "running": state["running"], "service": "connected" if remote else "active" if state["running"] else "inactive", "accounts": accounts, "models": ["preview-codex", "preview-claude"], "providers": providers, "autostart": state["autostart"], "endpoint": "https://proxy.example.invalid/v1" if remote else "http://127.0.0.1:0/v1", "version": state["installed"], "error": "", "quotas": quotas, "mode": mode, "connection_id": "remote-preview" if remote else "local", "base_url": "https://proxy.example.invalid" if remote else "", "remote_base_url": "https://proxy.example.invalid", "has_api_key": state.get("has_api_key", True)}
    elif command == "quotas":
        result = {"quotas": quotas}
        if "--notify" in args:
            result["alerts"] = {"error": "Preview alert delivery failed; no desktop notification was sent.", "sent": 0}
    elif command == "auth-status":
        result = {"auth": {"status": "none"}}
    elif command == "preferences":
        result = {"preferences": {"routing": state["routing"]}, "routing_settings": routing}
    elif command in ("check-updates", "backend-update", "backend-rollback"):
        if command != "check-updates":
            state.update(installed="v6.9.22" if command == "backend-update" else "v6.9.20", rollback=command == "backend-update")
            updates.update(installed_version=state["installed"], rollback_available=state["rollback"], update_available=command == "backend-rollback")
        result = {"updates": updates, "message": "Preview backend " + command + " completed."}
    elif command in ("routing-settings", "routing-save"):
        if payload:
            values.update(payload.get("changes", payload))
            state["routing"] = values["strategy"]
            state["routing_values"] = values
        result = {"routing_settings": routing, "message": "Preview routing settings loaded."}
    elif command == "routing":
        state["routing"] = args[1]
        values["strategy"] = state["routing"]
        result = {"preferences": {"routing": state["routing"]}, "routing_settings": routing, "message": "Preview routing changed."}
    elif command in ("diagnostics", "capture-activity"):
        if command == "capture-activity":
            event = {"request_label": "request-preview", "account_label": "account-preview", "client_label": "client-0000000000000001", "model_label": "model-preview", "provider": "codex", "timestamp": "2026-10-03T18:00:00Z", "status_code": 200, "outcome": "success", "latency_ms": 280, "ttft_ms": 40, "tokens": {"input_tokens": 100, "output_tokens": 23, "cached_tokens": 0, "total_tokens": 123}}
            if clients:
                event["client_name"] = clients[0]["name"]
            diagnostics["queue"] = {"availability": "available", "events": [event], "retained": 1, "invalid": 2, "omitted": 4, "capture_requested": True, "consumed": True}
            diagnostics["client_attribution"] = "receipt_fields_only"
        result = {"diagnostics": diagnostics}
    elif command.startswith("custom-"):
        if command in ("custom-save", "custom-add"):
            name = payload["name"]
            current = next((p for p in custom if p["name"] == name), None)
            provider = {"name": name, "url": payload.get("url", current["url"] if current else ""), "models": payload.get("models", current["models"] if current else []), "credential_count": current["credential_count"] if current else 1, "weights": current["weights"] if current else [1]}
            if "weight" in payload:
                provider["weights"] = list(provider["weights"])
                provider["weights"][payload.get("credential_index", 0)] = payload["weight"]
            custom = [p for p in custom if p["name"] != name] + [provider]
        elif command == "custom-remove":
            custom = [p for p in custom if p["name"] != args[1]]
        state["custom"] = custom
        for provider in custom:
            provider["credentials"] = [{"index": i, "has_key": True, "weight": weight} for i, weight in enumerate(provider["weights"])]
        result = {"custom_providers": custom, "provider_weights_supported": True, "message": "Preview provider action completed."}
        if command in ("custom-save", "custom-add"):
            result["custom_provider"] = next(p for p in custom if p["name"] == payload["name"])
    elif command in ("client-keys", "client-create", "client-copy", "client-revoke"):
        if command == "client-create" and not any(row["name"] == args[1] for row in clients):
            clients = clients + [{"name": args[1], "key_label": "client-0000000000000001", "active": True}]
        elif command == "client-revoke":
            clients = [row for row in clients if row["name"] != args[1]]
        state["client_keys"] = clients
        result = {"client_keys": clients, "message": "Preview client key " + command + " completed."}
        if command == "client-copy":
            result.update(copied=True, message="Preview client key copy recorded; clipboard unchanged.")
    elif command == "connection-save":
        state.update(mode="remote", has_api_key=False if payload.get("clear_api_key") else bool(payload.get("api_key")) or state.get("has_api_key", True))
        result = {"connection_changed": True, "connection_id": "remote-preview", "mode": "remote", "base_url": "https://proxy.example.invalid", "has_api_key": state["has_api_key"], "message": "Preview remote connection saved."}
    elif command == "connection-local":
        state["mode"] = "local"
        result = {"connection_changed": True, "connection_id": "local", "mode": "local", "remote_base_url": "https://proxy.example.invalid", "message": "Preview local connection selected."}
    elif command in ("start", "stop", "restart"):
        state["running"] = command != "stop"
        result = {"message": "Preview proxy state changed."}
    elif command == "autostart":
        state["autostart"] = args[1] == "on"
        result = {"message": "Preview login setting changed."}
    elif command == "logs-view":
        result = {"logs": "Offline preview. No backend was started."}
    elif command == "copy":
        result = {"message": "Preview copy action recorded; clipboard unchanged."}
    else:
        result = {"message": "Preview action recorded: " + command}
    if command in ("backend-update", "backend-rollback", "routing-save", "routing", "start", "stop", "restart", "autostart", "custom-save", "custom-add", "custom-remove", "connection-save", "connection-local", "client-create", "client-revoke"):
        temporary = root / ("state-" + str(os.getpid()) + ".json")
        temporary.write_text(json.dumps(state))
        temporary.replace(state_file)
    result.setdefault("connection_id", "remote-preview" if state.get("mode") == "remote" else "local")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
