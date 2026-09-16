import asyncio
import inspect
import time
import os
import httpx
from typing import Any, Dict

from .agent import Agent
from .memory import MemoryStore
from .models import AgentRun
from .tools import ToolResult, ToolRegistry
from gateway import persistence


RUNS: dict[str, AgentRun] = {}
TASKS: dict[str, asyncio.Task] = {}
RUN_LIMIT = 200


def _serialize(run: AgentRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "goal": run.goal,
        "status": run.status,
        "user_id": run.user_id,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "messages": run.messages[-100:],
        "steps": [{
            "id": s.id, "name": s.name, "tool": s.tool, "status": s.status,
            "attempts": s.attempts, "result": s.result,
        } for s in run.steps],
        "output": run.output,
    }


def _serialize_step(step):
    return {"id":step.id,"name":step.name,"tool":step.tool,"args":step.args,"status":step.status,"result":step.result,"attempts":step.attempts}


def _owned(run: AgentRun | None, user: dict | None) -> bool:
    if not run:
        return False
    if run.user_id is None:
        return user is None
    return bool(user and user.get("sub") == run.user_id)


def create_agent_router(auth_dependency):
    from fastapi import APIRouter, Depends, HTTPException
    from pydantic import BaseModel, Field

    router = APIRouter(prefix="/api/agent", tags=["agent"])

    class RunRequest(BaseModel):
        goal: str = Field(min_length=3, max_length=4000)
        wait: bool = Field(default=False)

    class CancelRequest(BaseModel):
        reason: str = Field(default="cancelled by user", max_length=500)

    memory = MemoryStore()

    def build_agent(user: dict | None):
        registry = ToolRegistry()

        def remember(**kwargs: Any) -> ToolResult:
            event = dict(kwargs)
            event.pop("context", None)
            event["user_id"] = (user or {}).get("sub")
            memory.remember(event)
            return ToolResult(True, {"saved": True})

        async def analyze_request(**kwargs: Any) -> ToolResult:
            from gateway.server import Analyze, analyze
            ctx = kwargs.get("context") or {}
            result = await analyze(Analyze(prompt=str(kwargs.get("prompt", ""))), None, user)
            return ToolResult(True, result if isinstance(result, dict) else {"result": result})

        async def generate_video(**kwargs: Any) -> ToolResult:
            from gateway.server import Generate, generate
            ctx = kwargs.get("context") or {}
            goal = str(kwargs.get("prompt") or ctx.get("goal") or "")
            # Retrieve a small set of user-owned creative patterns. The agent adapts from
            # successful/rated outputs; it does not modify a third-party foundation model.
            uid=(user or {}).get("sub") if isinstance(user,dict) else None
            if uid and persistence.enabled():
                try:
                    rows=await persistence.sb_request("GET","brain_patterns",params={"user_id":f"eq.{uid}","select":"pattern","order":"created_at.desc","limit":"5"})
                    cues=[]
                    for row in rows or []:
                        pat=row.get("pattern") or {}; tags=pat.get("tags") or []
                        if tags: cues.extend(str(t) for t in tags[:6])
                    if cues: goal += "\nUse these learned creative cues where appropriate: " + ", ".join(dict.fromkeys(cues))
                except Exception: pass
            duration_match = __import__("re").search(r"\b([1-9][0-9]?)\s*(?:s|sec|seconds)\b", goal.lower())
            duration = f"{duration_match.group(1)}s" if duration_match else "5s"
            req = Generate(prompt=goal, mode="Text → Video", duration=duration)
            result = await generate(req, None, user)
            return ToolResult(True, dict(result))

        async def wait_for_generation(**kwargs: Any) -> ToolResult:
            from gateway.server import status
            ctx = kwargs.get("context") or {}
            gen = ctx.get("generate_primary_asset") or {}
            prompt_id = gen.get("promptId")
            if not prompt_id:
                return ToolResult(False, {}, "No generation prompt ID returned")
            deadline = time.time() + 90
            last = None
            while time.time() < deadline:
                last = await status(str(prompt_id), None, user)
                if last.get("status") in ("complete", "error"):
                    return ToolResult(last.get("status") == "complete", last, last.get("error"))
                await asyncio.sleep(1.0)
            return ToolResult(False, last or {}, "Generation status polling timed out")

        async def quality_check_media(**kwargs: Any) -> ToolResult:
            ctx = kwargs.get("context") or {}
            generation = ctx.get("wait_for_generation") or {}
            if generation.get("status") != "complete":
                return ToolResult(False, {"passed": False}, "Generation did not complete successfully")
            url = generation.get("videoUrl")
            if not url:
                return ToolResult(False, {"passed": False}, "Completed generation has no output URL")
            import shutil, tempfile, subprocess, json as _json
            if not shutil.which('ffprobe'):
                return ToolResult(False, {"passed": False}, "ffprobe is required for production media QA")
            tmp = None
            try:
                async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
                    async with client.stream('GET', url) as resp:
                        if resp.status_code >= 400:
                            return ToolResult(False, {"passed": False}, f"Output media fetch failed ({resp.status_code})")
                        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as fh:
                            tmp=fh.name
                            async for chunk in resp.aiter_bytes(1024*1024): fh.write(chunk)
                probe=subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',tmp],capture_output=True,text=True,timeout=60)
                if probe.returncode != 0:
                    return ToolResult(False, {"passed": False}, "ffprobe could not decode the generated media")
                meta=_json.loads(probe.stdout or '{}')
                streams=meta.get('streams') or []
                video=next((x for x in streams if x.get('codec_type')=='video'),None)
                if not video:
                    return ToolResult(False, {"passed": False}, "Generated output contains no video stream")
                duration=float((meta.get('format') or {}).get('duration') or video.get('duration') or 0)
                if duration <= 0 or int(video.get('width') or 0) <= 0 or int(video.get('height') or 0) <= 0:
                    return ToolResult(False, {"passed": False}, "Generated media has invalid duration or dimensions")
                return ToolResult(True, {"passed": True, "checks": ["generation_complete","output_url_present","ffprobe_decode","video_stream","positive_duration","valid_dimensions"], "videoUrl": url, "duration": duration, "width": video.get('width'), "height": video.get('height')})
            except Exception as exc:
                return ToolResult(False, {"passed": False}, f"Media QA failed: {exc}")
            finally:
                if tmp:
                    try: os.unlink(tmp)
                    except Exception: pass

        async def learn_output(**kwargs: Any) -> ToolResult:
            from gateway.brain_learning import LearnOutputRequest, learn_output
            ctx = kwargs.get("context") or {}
            generation = ctx.get("wait_for_generation") or {}
            url = str(kwargs.get("output_url") or generation.get("videoUrl") or "")
            if not url:
                return ToolResult(False, {}, "No output URL available for learning")
            uid=(user or {}).get("sub") if isinstance(user,dict) else None
            if not uid:
                return ToolResult(False, {}, "Authenticated user is required for Brain learning")
            req=LearnOutputRequest(output_url=url, source_prompt=str(ctx.get("goal") or ""), rating=int(kwargs.get("rating",0) or 0), accepted=bool(kwargs.get("accepted",False)), tags=list(kwargs.get("tags") or []))
            try:
                return ToolResult(True, await learn_output(req, uid))
            except Exception as exc:
                return ToolResult(False, {}, str(exc))

        async def generate_series(**kwargs: Any) -> ToolResult:
            from gateway.server import Generate, generate
            goal=str(kwargs.get("prompt") or (kwargs.get("context") or {}).get("goal") or "")
            count=max(2,min(int(kwargs.get("episodes",3)),12))
            duration=str(kwargs.get("duration","5s"))
            model=str(kwargs.get("model","auto"))
            outputs=[]
            for i in range(1,count+1):
                episode_prompt=f"{goal}. Create episode {i} of {count}. Preserve character, visual identity, world, wardrobe, lighting and camera language across the series."
                r=await generate(Generate(prompt=episode_prompt,mode="Text → Video",duration=duration,model=model),None,user)
                outputs.append(r)
            return ToolResult(True,{"series_count":count,"jobs":outputs,"series_prompt":goal,"learning_mode":"retrieval_and_adaptation"})

        async def project_status(**kwargs: Any) -> ToolResult:
            from gateway.server import providers
            result = await providers(None, user)
            return ToolResult(True, result)

        registry.register("echo", "Safe local plan step.", lambda **kw: ToolResult(True, {"message": str(kw.get("message", ""))[:4000]}))
        registry.register("analyze_request", "Reuse V12 authenticated request analysis/fallback classification.", analyze_request)
        registry.register("generate_video", "Start generation through V12's canonical provider router.", generate_video)
        registry.register("wait_for_generation", "Poll V12 generation status until complete, error or timeout.", wait_for_generation)
        def quality_check(**kw):
            required = kw.get("required_fields") or []
            payload = kw.get("payload") or {}
            missing = [k for k in required if not isinstance(payload, dict) or not payload.get(k)]
            if missing:
                return ToolResult(False, {"passed": False, "missing": missing}, "Missing required fields")
            return ToolResult(True, {"passed": True, "checks": ["required_fields"]})
        registry.register("quality_check", "Validate required planning fields.", quality_check)
        registry.register("quality_check_media", "Validate that the generation completed and returned an output URL.", quality_check_media)
        registry.register("learn_output", "Sample and analyze generated media, then persist reusable creative patterns with provenance.", learn_output)
        registry.register("generate_series", "Generate a multi-episode AI video series using the canonical V12 generation router and continuity instructions.", generate_series)
        registry.register("remember", "Persist a user-scoped agent event.", remember)
        registry.register("project_status", "Report configured provider state without leaking secrets.", project_status)
        return Agent(registry=registry, memory=memory)

    async def worker(run: AgentRun, goal: str, user: dict | None):
        try:
            result = await build_agent(user).run_async(goal, user_id=(user or {}).get("sub"), context={})
            result.run_id = run.run_id
            RUNS[run.run_id] = result
            if persistence.enabled() and result.user_id:
                try: await persistence.update_agent_run(run.run_id,status=result.status,plan=[_serialize_step(x) for x in result.steps],output=result.output,error=(result.output or {}).get('error') if result.status=='failed' else None)
                except Exception: pass
        except asyncio.CancelledError:
            run.status = "cancelled"
            run.messages.append("Agent task cancelled")
            run.updated_at = time.time()
            if persistence.enabled() and run.user_id:
                try: await persistence.update_agent_run(run.run_id,status='cancelled',plan=[_serialize_step(x) for x in run.steps],output=run.output)
                except Exception: pass
            raise
        except Exception as exc:
            run.status = "failed"
            run.output = {"error": str(exc)}
            run.messages.append(f"Agent worker failed: {exc}")
            run.updated_at = time.time()
        finally:
            TASKS.pop(run.run_id, None)

    @router.get("/health", dependencies=[Depends(auth_dependency)])
    async def health() -> Dict[str, Any]:
        return {"ok": True, "agent": "vidigen-v12-orchestrator", "version": "2.0.0"}

    @router.get("/tools", dependencies=[Depends(auth_dependency)])
    async def tools(user=Depends(auth_dependency)) -> Dict[str, Any]:
        return {"tools": build_agent(user).registry.list()}

    @router.post("/run")
    async def run(req: RunRequest, user=Depends(auth_dependency)) -> Dict[str, Any]:
        agent = build_agent(user)
        try:
            plan = agent.plan(req.goal)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        run_id = __import__("uuid").uuid4().hex
        now = time.time()
        run = AgentRun(run_id, plan.goal, "queued", plan.steps, user_id=(user or {}).get("sub"), created_at=now, updated_at=now)
        RUNS[run_id] = run
        if persistence.enabled() and (user or {}).get('sub'):
            try: await persistence.create_agent_run(run_id,(user or {}).get('sub'),plan.goal,'queued',[_serialize_step(s) for s in plan.steps])
            except Exception: pass
        task = asyncio.create_task(worker(run, req.goal, user), name=f"vidigen-agent-{run_id}")
        TASKS[run_id] = task
        while len(RUNS) > RUN_LIMIT:
            oldest = min(RUNS.values(), key=lambda x: x.updated_at)
            if oldest.run_id == run_id:
                break
            RUNS.pop(oldest.run_id, None)
        if req.wait:
            await task
        return _serialize(RUNS.get(run_id, run))

    @router.get("/runs/{run_id}")
    async def run_status(run_id: str, user=Depends(auth_dependency)) -> Dict[str, Any]:
        run = RUNS.get(run_id)
        if not run or not _owned(run, user):
            raise HTTPException(404, "Agent run not found")
        return _serialize(run)

    @router.post("/runs/{run_id}/cancel")
    async def cancel(run_id: str, req: CancelRequest, user=Depends(auth_dependency)) -> Dict[str, Any]:
        run = RUNS.get(run_id)
        if not run or not _owned(run, user):
            raise HTTPException(404, "Agent run not found")
        task = TASKS.get(run_id)
        if task and not task.done():
            task.cancel()
            run.status = "cancelled"
            run.messages.append(req.reason)
            run.updated_at = time.time()
        return _serialize(run)

    return router
