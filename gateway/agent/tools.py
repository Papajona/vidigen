from __future__ import annotations
import asyncio
import inspect
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional


@dataclass
class ToolResult:
    ok: bool
    data: Dict[str, Any]
    error: Optional[str] = None


@dataclass
class Tool:
    name: str
    description: str
    handler: Callable[..., Any]


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, name: str, description: str, handler: Callable[..., Any]) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise ValueError("Invalid agent tool name")
        self._tools[name] = Tool(name, description, handler)

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"Unknown agent tool: {name}")
        return self._tools[name]

    def list(self) -> Dict[str, str]:
        return {name: tool.description for name, tool in self._tools.items()}

    async def call_async(self, name: str, **kwargs: Any) -> ToolResult:
        result = self.get(name).handler(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ToolResult):
            raise TypeError(f"Agent tool {name} returned an invalid result")
        return result

    def call(self, name: str, **kwargs: Any) -> ToolResult:
        result = self.get(name).handler(**kwargs)
        if inspect.isawaitable(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                raise RuntimeError("Use call_async() when an event loop is running")
            return asyncio.run(result)
        if not isinstance(result, ToolResult):
            raise TypeError(f"Agent tool {name} returned an invalid result")
        return result


def _echo_task(**kwargs: Any) -> ToolResult:
    message = str(kwargs.get("message", ""))[:4000]
    return ToolResult(ok=True, data={"message": message, "mode": "local-safe"})


def _quality_check(**kwargs: Any) -> ToolResult:
    required = kwargs.get("required_fields", [])
    payload = kwargs.get("payload", {})
    if not isinstance(required, list) or not isinstance(payload, dict):
        return ToolResult(False, {"passed": False}, "Invalid quality-check input")
    missing = [k for k in required if not payload.get(k)]
    if missing:
        return ToolResult(False, {"passed": False, "missing": missing}, f"Missing required fields: {', '.join(missing)}")
    return ToolResult(True, {"passed": True, "checks": ["required_fields"]})


def _local_analysis(**kwargs: Any) -> ToolResult:
    prompt = str(kwargs.get("prompt", "")).strip()
    lower = prompt.lower()
    media_type = "video" if any(w in lower for w in ("video", "clip", "reel", "advert", "trailer")) else "text"
    return ToolResult(True, {"type": media_type, "summary": prompt[:500], "mode": "deterministic-fallback"})


def _production_unavailable(**kwargs: Any) -> ToolResult:
    return ToolResult(False, {"production": True}, "No canonical Vidigen provider is available for this operation.")

def _production_media_unavailable(**kwargs: Any) -> ToolResult:
    return ToolResult(False, {"production": True}, "Production media QA requires a completed canonical V12 generation output.")


def build_default_registry(memory=None) -> ToolRegistry:
    from .memory import MemoryStore
    mem = memory or MemoryStore()
    r = ToolRegistry()
    r.register("analyze_request", "Deterministic local request classification fallback.", _local_analysis)
    r.register("generate_video", "Production fallback: refuse generation without a canonical provider.", _production_unavailable)
    r.register("wait_for_generation", "Production fallback: canonical V12 API performs generation polling.", _production_unavailable)
    r.register("quality_check", "Validate required fields before a step can pass.", _quality_check)
    r.register("quality_check_media", "Production fallback: canonical V12 Agent API performs media QA.", _production_media_unavailable)
    r.register("learn_output", "Production fallback: Brain learning is available only through the authenticated V12 Brain API.", _production_unavailable)
    r.register("generate_series", "Production fallback: series generation is available only through the authenticated V12 Agent API.", _production_unavailable)
    r.register("remember", "Persist a user-scoped agent event in fallback memory.", lambda **kw: _remember(mem, **kw))
    r.register("project_status", "Return non-secret Vidigen provider configuration status.", _project_status)
    return r


def _remember(memory, **kwargs: Any) -> ToolResult:
    event = dict(kwargs)
    try:
        memory.remember(event)
        return ToolResult(True, {"saved": True})
    except Exception as exc:
        return ToolResult(False, {"saved": False}, str(exc))


def _project_status(**kwargs: Any) -> ToolResult:
    return ToolResult(True, {
        "project": "vidigen",
        "provider_mode": os.getenv("VIDEO_PROVIDER", "replicate"),
        "replicate_configured": bool(os.getenv("REPLICATE_API_TOKEN")) and bool(os.getenv("REPLICATE_MODEL")),
        "seedance_configured": bool(os.getenv("SEEDANCE_API_URL")) and bool(os.getenv("SEEDANCE_API_TOKEN")),
        "runway_configured": bool(os.getenv("RUNWAY_API_URL")) and bool(os.getenv("RUNWAY_API_TOKEN")),
    })
