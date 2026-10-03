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
        stream.write(json.dumps({"command": command, "argument_count": len(args) - 1, "payload_fields": sorted(payload or {})}) + "\n")
    providers = [{"id": "codex", "name": "OpenAI Codex"}, {"id": "claude", "name": "Claude"}, {"id": "gemini", "name": "Gemini"}]
    accounts = [{"name": f"preview-{p['id']}.json", "auth_index": f"fixture-{p['id']}", "provider": p["id"], "email": f"preview-{p['id']}@example.invalid", "plan": "pro" if p["id"] == "codex" else "max", "disabled": False, "status": "ready", "success": 12, "failed": 1} for p in providers]
    quotas = {"accounts": [{"name": a["name"], "provider": a["provider"], "email": a["email"], "plan": a["plan"], "available": True, "windows": [{"label": "Weekly", "remaining_percent": 72, "reset_at": time.time() + 86400}, {"label": "5 hours", "remaining_percent": 43, "reset_at": time.time() + 3600}]} for a in accounts]}
    updates = {"installed_version": state["installed"], "latest_version": "v6.9.22", "reviewed_version": "v6.9.22", "update_supported": True, "update_available": state["installed"] != "v6.9.22", "rollback_available": state["rollback"], "error": ""}
    values = {"strategy": state["routing"], "session-affinity": True, "session-affinity-ttl": "1h", "session-affinity-subagents": True, "request-retry": 2, "max-retry-credentials": 2, "max-retry-interval": 30, "disable-cooling": False, "save-cooldown-status": True}
    values.update(state.get("routing_values", {}))
    values["strategy"] = state["routing"]
    routing = {"values": values, "weights": True, "capabilities": {k: True for k in values}, "strategies": ["round-robin", "fill-first", "weighted-round-robin"], "limitations": ["Offline preview: changes are stored only in temporary fixture state."]}
    records = [{"label": "account-preview", "provider": "codex", "success": 12, "failed": 1}]
    diagnostics = {"usage": {"availability": "available", "records": records, "retained": 1, "invalid": 0, "omitted": 0}, "accounts": {"availability": "available", "records": records, "retained": 1, "invalid": 0, "omitted": 0}, "queue": {"availability": "not_consumed", "events": []}, "client_attribution": "unavailable", "limitations": ["Fixture counters describe backend attempts, not billing totals.", "Client identity and request routing are unavailable from aggregate counters."]}
    custom = state.get("custom", [{"name": "preview-provider", "url": "https://provider.example.invalid/v1", "models": [{"name": "preview-upstream", "alias": "preview-model"}], "credential_count": 2, "weights": [1, 2]}])
    if command == "status":
        mode = state.get("mode", "local")
        remote = mode == "remote"
        result = {"configured": True, "running": state["running"], "service": "connected" if remote else "active" if state["running"] else "inactive", "accounts": accounts, "models": ["preview-codex", "preview-claude"], "providers": providers, "autostart": state["autostart"], "endpoint": "https://proxy.example.invalid/v1" if remote else "http://127.0.0.1:0/v1", "version": state["installed"], "error": "", "quotas": quotas, "mode": mode, "connection_id": "remote-preview" if remote else "local", "base_url": "https://proxy.example.invalid" if remote else "", "remote_base_url": "https://proxy.example.invalid", "has_api_key": state.get("has_api_key", True)}
    elif command == "quotas":
        result = {"quotas": quotas}
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
            diagnostics["queue"] = {"availability": "available", "events": [{"request_label": "request-preview", "account_label": "account-preview", "client_label": "client-preview", "model_label": "model-preview", "outcome": "success", "latency_ms": 280, "ttft_ms": 40, "tokens": {"total_tokens": 123}}], "retained": 1, "invalid": 0, "omitted": 0}
        result = {"diagnostics": diagnostics}
    elif command.startswith("custom-"):
        if command in ("custom-save", "custom-add"):
            name = payload["name"]
            current = next((p for p in custom if p["name"] == name), None)
            provider = {"name": name, "url": payload["url"], "models": payload["models"], "credential_count": current["credential_count"] if current else 1, "weights": current["weights"] if current else [1]}
            if "weight" in payload:
                provider["weights"] = list(provider["weights"])
                provider["weights"][payload.get("credential_index", 0)] = payload["weight"]
            custom = [p for p in custom if p["name"] != name] + [provider]
        elif command == "custom-remove":
            custom = [p for p in custom if p["name"] != args[1]]
        state["custom"] = custom
        result = {"custom_providers": custom, "message": "Preview provider action completed."}
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
    if command in ("backend-update", "backend-rollback", "routing-save", "routing", "start", "stop", "restart", "autostart", "custom-save", "custom-add", "custom-remove", "connection-save", "connection-local"):
        temporary = root / ("state-" + str(os.getpid()) + ".json")
        temporary.write_text(json.dumps(state))
        temporary.replace(state_file)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
