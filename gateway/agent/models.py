from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TaskStep:
    id: str
    name: str
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    result: Optional[Dict[str, Any]] = None
    attempts: int = 0


@dataclass
class AgentPlan:
    goal: str
    steps: List[TaskStep]
    assumptions: List[str] = field(default_factory=list)


@dataclass
class AgentRun:
    run_id: str
    goal: str
    status: str
    steps: List[TaskStep]
    messages: List[str] = field(default_factory=list)
    output: Optional[Dict[str, Any]] = None
    user_id: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0
