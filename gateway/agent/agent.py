from __future__ import annotations
import asyncio
import inspect
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .memory import MemoryStore
from .models import AgentPlan, AgentRun
from .planner import plan_goal
from .tools import ToolRegistry, build_default_registry, ToolResult


@dataclass
class AgentConfig:
    max_retries: int = 2
    retry_delay_seconds: float = 0.25
    remember_runs: bool = True
    max_steps: int = 20


class Agent:
    def __init__(self, registry: Optional[ToolRegistry] = None, memory: Optional[MemoryStore] = None, config: Optional[AgentConfig] = None):
        self.memory = memory or MemoryStore()
        self.registry = registry or build_default_registry(self.memory)
        self.config = config or AgentConfig()

    def plan(self, goal: str) -> AgentPlan:
        plan = plan_goal(goal)
        if len(plan.steps) > self.config.max_steps:
            raise ValueError("Agent plan exceeds safety step limit")
        for step in plan.steps:
            self.registry.get(step.tool)  # fail closed on unknown tools
        return plan

    def run(self, goal: str, user_id: str | None = None, context: dict[str, Any] | None = None) -> AgentRun:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            raise RuntimeError("Use run_async() when an event loop is running")
        return asyncio.run(self.run_async(goal, user_id=user_id, context=context))

    async def run_async(self, goal: str, user_id: str | None = None, context: dict[str, Any] | None = None) -> AgentRun:
        run_id = uuid.uuid4().hex
        now = time.time()
        try:
            plan = self.plan(goal)
        except Exception as exc:
            run = AgentRun(run_id, str(goal or ""), "failed", [], [f"Planning failed: {exc}"], {"error": str(exc)}, user_id, now, time.time())
            if self.config.remember_runs:
                self.memory.remember({"type": "agent_run", "run_id": run_id, "goal": str(goal or ""), "status": "failed", "user_id": user_id, "error": str(exc)})
            return run

        run = AgentRun(run_id, plan.goal, "running", plan.steps, [], None, user_id, now, time.time())
        ctx = dict(context or {})
        ctx.setdefault("run_id", run_id)
        ctx.setdefault("user_id", user_id)
        ctx.setdefault("goal", plan.goal)

        for step in run.steps:
            step.status = "running"
            success = False
            last_error = "unknown tool error"
            for attempt in range(1, self.config.max_retries + 2):
                step.attempts = attempt
                args = dict(step.args)
                args.setdefault("context", ctx)
                try:
                    result = await self.registry.call_async(step.tool, **args)
                    if result.ok:
                        step.status = "completed"
                        step.result = result.data
                        run.messages.append(f"{step.id}: {step.name} completed")
                        ctx[step.name] = result.data
                        success = True
                        break
                    last_error = result.error or "tool returned failure"
                except Exception as exc:
                    last_error = str(exc)
                run.messages.append(f"{step.id}: attempt {attempt} failed: {last_error}")
                if attempt <= self.config.max_retries:
                    await asyncio.sleep(self.config.retry_delay_seconds)

            if not success:
                step.status = "failed"
                step.result = {"error": last_error}
                run.status = "failed"
                run.updated_at = time.time()
                if self.config.remember_runs:
                    self.memory.remember({"type": "agent_run", "run_id": run_id, "goal": plan.goal, "status": run.status, "user_id": user_id, "failed_step": step.id, "error": last_error})
                return run
            run.updated_at = time.time()

        run.status = "completed"
        run.output = {"message": "Agent workflow completed.", "run_id": run_id, "completed_steps": len(run.steps)}
        run.updated_at = time.time()
        if self.config.remember_runs:
            self.memory.remember({"type": "agent_run", "run_id": run_id, "goal": plan.goal, "status": run.status, "user_id": user_id, "completed_steps": len(run.steps)})
        return run
