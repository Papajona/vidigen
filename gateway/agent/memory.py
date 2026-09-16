from __future__ import annotations
import json
import os
import time
import threading
from pathlib import Path
from typing import Any, Dict, List


class MemoryStore:
    """Bounded local fallback memory.

    Production use should pass a user/project-scoped store backed by Supabase. The
    local store deliberately supports a user_id field so multiple local users do not
    silently share profile events in future integrations.
    """

    def __init__(self, path: str = "gateway/data/agent_memory.json"):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"profile": {}, "events": []})

    def _read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"profile": {}, "events": []}
        except (OSError, json.JSONDecodeError):
            return {"profile": {}, "events": []}

    def _write(self, data: Dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def set_preference(self, key: str, value: Any, user_id: str | None = None) -> None:
        if not key or len(key) > 100:
            raise ValueError("Invalid memory preference key")
        with self._lock:
            data = self._read()
            if user_id:
                profiles = data.setdefault("profiles", {})
                profile = profiles.setdefault(user_id, {})
                profile[key] = value
            else:
                data.setdefault("profile", {})[key] = value
            self._write(data)

    def get_preferences(self, user_id: str | None = None) -> Dict[str, Any]:
        data = self._read()
        if user_id:
            return dict(data.get("profiles", {}).get(user_id, {}))
        return dict(data.get("profile", {}))

    def remember(self, event: Dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            event = dict(event)
            event.setdefault("timestamp", time.time())
            data.setdefault("events", []).append(event)
            data["events"] = data["events"][-500:]
            self._write(data)

    def recent(self, limit: int = 20, user_id: str | None = None) -> List[Dict[str, Any]]:
        limit = max(0, min(int(limit), 500))
        events = data_events = self._read().get("events", [])
        if user_id:
            events = [e for e in data_events if e.get("user_id") == user_id]
        return events[-limit:]

    def search(self, query: str, limit: int = 10, user_id: str | None = None) -> List[Dict[str, Any]]:
        q = str(query or "").lower().strip()
        if not q:
            return []
        hits = []
        for event in self._read().get("events", []):
            if user_id and event.get("user_id") != user_id:
                continue
            blob = json.dumps(event, ensure_ascii=False).lower()
            if q in blob:
                hits.append(event)
        return hits[-max(0, min(int(limit), 100)):]
