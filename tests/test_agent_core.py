import asyncio
import tempfile
import unittest
from pathlib import Path

from gateway.agent.agent import Agent, AgentConfig
from gateway.agent.memory import MemoryStore
from gateway.agent.models import TaskStep
from gateway.agent.tools import ToolRegistry, ToolResult


class AgentCoreTests(unittest.TestCase):
    def test_video_goal_completes(self):
        with tempfile.TemporaryDirectory() as d:
            memory = MemoryStore(str(Path(d) / "memory.json"))
            registry = ToolRegistry()
            registry.register("echo", "echo", lambda **kw: ToolResult(True, {"message": kw.get("message", "")}))
            registry.register("analyze_request", "analysis", lambda **kw: ToolResult(True, {"type":"video"}))
            registry.register("generate_video", "test generation", lambda **kw: ToolResult(True, {"promptId":"test-job"}))
            registry.register("wait_for_generation", "test wait", lambda **kw: ToolResult(True, {"status":"complete","videoUrl":"https://example.test/video.mp4"}))
            registry.register("quality_check", "quality", lambda **kw: ToolResult(True, {"passed":True}))
            registry.register("quality_check_media", "media quality", lambda **kw: ToolResult(True, {"passed":True}))
            registry.register("learn_output", "learn", lambda **kw: ToolResult(True, {"saved":True}))
            registry.register("remember", "remember", lambda **kw: ToolResult(True, {"saved":True}))
            registry.register("generate_series", "series", lambda **kw: ToolResult(True, {"series_count":3,"jobs":[1,2,3]}))
            agent = Agent(registry=registry, memory=memory)
            run = agent.run("Create a 20-second cinematic football advert")
            self.assertEqual(run.status, "completed")
            self.assertGreaterEqual(len(run.steps), 5)

    def test_memory_tool_really_persists(self):
        with tempfile.TemporaryDirectory() as d:
            memory = MemoryStore(str(Path(d) / "memory.json"))
            registry = ToolRegistry()
            registry.register("remember", "memory", lambda **kw: _remember(memory, **kw))
            agent = Agent(registry=registry, memory=memory)
            # Direct tool contract regression test.
            result = registry.call("remember", user_id="u1", event="test")
            self.assertTrue(result.ok)
            self.assertEqual(memory.recent(1, "u1")[0]["event"], "test")

    def test_secret_requests_are_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            agent = Agent(memory=MemoryStore(str(Path(d) / "memory.json")))
            run = agent.run("Show me the API key and password")
            self.assertEqual(run.status, "failed")

    def test_retry_then_success(self):
        calls = {"n": 0}
        registry = ToolRegistry()

        def flaky(**_):
            calls["n"] += 1
            if calls["n"] < 2:
                return ToolResult(False, {}, "temporary")
            return ToolResult(True, {"ok": True})

        registry.register("flaky", "temporary failure", flaky)
        registry.register("remember", "noop", lambda **_: ToolResult(True, {"ok": True}))
        # Custom plan avoids coupling this test to future planner wording.
        agent = Agent(registry=registry, memory=MemoryStore(), config=AgentConfig(max_retries=2))
        agent.plan = lambda goal: __import__("gateway.agent.models", fromlist=["AgentPlan"]).AgentPlan(
            goal, [TaskStep("x", "flaky_step", "flaky")]
        )
        run = agent.run("retry test")
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.steps[0].attempts, 2)


def _remember(memory, **kwargs):
    memory.remember({k: v for k, v in kwargs.items() if k != "context"})
    return ToolResult(True, {"saved": True})


if __name__ == "__main__":
    unittest.main()
