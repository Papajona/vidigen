import os
import asyncio
import unittest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("VIDIGEN_GATEWAY_TOKEN", "test-token")
os.environ.setdefault("WORKFLOW_FILE", "/tmp/vidigen-test-missing-workflow.json")

from gateway.server import app


class AgentApiTests(unittest.TestCase):
    def test_agent_auth_and_health(self):
        async def run():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                r = await c.get("/api/agent/health")
                self.assertEqual(r.status_code, 401)
                r = await c.get("/api/agent/health", headers={"Authorization": "Bearer test-token"})
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r.json()["ok"])
        asyncio.run(run())


    def test_video_goal_fails_closed_when_no_provider_or_workflow(self):
        async def run():
            import os
            os.environ.pop("REPLICATE_API_TOKEN", None)
            os.environ.pop("REPLICATE_MODEL", None)
            os.environ.pop("SEEDANCE_API_URL", None)
            os.environ.pop("SEEDANCE_API_TOKEN", None)
            os.environ.pop("RUNWAY_API_URL", None)
            os.environ.pop("RUNWAY_API_TOKEN", None)
            os.environ["WORKFLOW_FILE"] = "/tmp/vidigen-test-missing-workflow.json"
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                h = {"Authorization": "Bearer test-token"}
                r = await c.post("/api/agent/run", headers=h, json={"goal": "Create a 5-second football trailer", "wait": True})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["status"], "failed")
        asyncio.run(run())

    def test_agent_run_and_ownership(self):
        async def run():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                h = {"Authorization": "Bearer test-token"}
                r = await c.post("/api/agent/run", headers=h, json={"goal": "Review this project request", "wait": True})
                self.assertEqual(r.status_code, 200)
                data = r.json()
                self.assertEqual(data["status"], "completed")
                rid = data["run_id"]
                r2 = await c.get(f"/api/agent/runs/{rid}", headers=h)
                self.assertEqual(r2.status_code, 200)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
