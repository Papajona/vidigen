from __future__ import annotations
import re
from typing import List
from .models import AgentPlan, TaskStep

_VIDEO_WORDS = ("video", "clip", "reel", "advert", "ad", "trailer", "tiktok", "short")
_BLOCKED = re.compile(r"\b(password|api\s*key|secret|private\s*key|token)\b", re.I)
_ABUSE = re.compile(r"\b(bypass|exploit|malware|ransomware|steal|phish)\b", re.I)


def _has_video_intent(goal: str) -> bool:
    g = goal.lower()
    return any(w in g for w in _VIDEO_WORDS)

def _has_series_intent(goal: str) -> bool:
    g=goal.lower()
    return 'series' in g or bool(re.search(r'\b(episode|episodes)\b',g))


def _clean_goal(goal: str) -> str:
    clean = re.sub(r"\s+", " ", str(goal or "")).strip()
    if not clean:
        raise ValueError("Agent goal cannot be empty")
    if len(clean) > 4000:
        raise ValueError("Agent goal is too long")
    return clean


def plan_goal(goal: str) -> AgentPlan:
    clean = _clean_goal(goal)
    if _BLOCKED.search(clean):
        raise ValueError("Agent blocked a credential/secret request")
    if _ABUSE.search(clean):
        raise ValueError("Agent blocked a prohibited security-abuse request")

    steps: List[TaskStep] = [
        TaskStep("s1", "understand_request", "echo", {"message": clean}),
        TaskStep("s2", "analyze_request", "analyze_request", {"prompt": clean}),
    ]
    if _has_video_intent(clean):
        if _has_series_intent(clean):
            m=re.search(r'\b(\d{1,2})\s*(?:episode|episodes)\b',clean,re.I)
            episodes=int(m.group(1)) if m else 3
            steps.extend([
                TaskStep("s3", "create_production_brief", "echo", {"message": "Create a production brief with continuity requirements and episode-level beats."}),
                TaskStep("s4", "quality_gate_brief", "quality_check", {"required_fields": ["message"], "payload": {"message": clean}}),
                TaskStep("s5", "generate_series", "generate_series", {"prompt": clean, "episodes": episodes}),
                TaskStep("s6", "quality_gate_series_jobs", "quality_check", {"required_fields": ["series_count", "jobs"], "payload": {"series_count": episodes, "jobs": ["created"]}}),
                TaskStep("s7", "save_series_memory", "remember", {"event": "series_generation_requested"}),
            ])
        else:
            steps.extend([
                TaskStep("s3", "create_production_brief", "echo", {"message": "Create a structured brief: objective, audience, duration, ratio, style and CTA."}),
                TaskStep("s4", "quality_gate_brief", "quality_check", {"required_fields": ["message"], "payload": {"message": clean}}),
                TaskStep("s5", "generate_primary_asset", "generate_video", {"prompt": clean}),
                TaskStep("s6", "wait_for_generation", "wait_for_generation", {}),
                TaskStep("s7", "validate_generation", "quality_check_media", {}),
                TaskStep("s8", "learn_output", "learn_output", {"rating": 0, "accepted": False}),
            ])
    else:
        steps.append(TaskStep("s3", "prepare_response", "echo", {"message": clean}))

    steps.append(TaskStep("s_final", "save_run_memory", "remember", {"event": "agent_run_finished"}))
    return AgentPlan(goal=clean, steps=steps, assumptions=["Existing V12 provider/router contracts are authoritative."])
