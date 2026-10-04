"""FastAPI routes over a small offline TripCrew recording: forks, reports, exports, errors."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
STALE_SEED = 11


async def _record(directory: Path) -> None:
    from agents.tripcrew import TravelAPI, TripCrew, generate_scenarios
    from agents.tripcrew.fixture_client import FixtureClient
    from blackbox.sdk import Recorder

    recorder = Recorder(directory, mode="offline", llm_client=FixtureClient())
    try:
        for seed, stale in ((7, False), (STALE_SEED, True)):
            scenarios = generate_scenarios(2, seed=seed)
            api = TravelAPI(scenarios, stale_fx=stale)
            for scenario in scenarios:
                with recorder.run(
                    "tripcrew", scenario.scenario_id, seed, model="tripcrew-fixture-v1"
                ) as run:
                    await TripCrew(scenario, api, model="tripcrew-fixture-v1")(run)
    finally:
        recorder.close()


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        data = Path(cls.directory.name)
        agent_dir = data / "tripcrew"
        asyncio.run(_record(agent_dir))
        (agent_dir / "replay.json").write_text(json.dumps({"stale_fx_seeds": [STALE_SEED]}))
        cls.saved_env = {key: os.environ.get(key) for key in ("DATA_DIR", "MODE", "CORS_ORIGINS")}
        # A trailing slash, as pasted from a browser address bar, must still match.
        os.environ.update(
            {"DATA_DIR": str(data), "MODE": "offline", "CORS_ORIGINS": "http://localhost:3000/"}
        )

        import server.app as app_module

        cls.client = TestClient(app_module.app, raise_server_exceptions=False)
        cls.client.__enter__()
        cls.service = app_module.app.state.service
        runs = cls.client.get("/runs", params={"limit": 50}).json()["items"]
        cls.failed = next(r["run_id"] for r in runs if r["status"] == "failed")

        from blackbox.forge.adapters import make_adapter

        agent = cls.service.agents["tripcrew"]
        adapter = make_adapter(
            agent.recorder,
            argparse.Namespace(agent="tripcrew", stale_fx=False, stale_fx_seeds=(STALE_SEED,)),
        )
        fresh_fx = adapter.oracle_fixes(cls.failed)["fx/tool#1"]
        edit = {"addr": "fx/tool#1", "kind": "patch_tool_result", "value": fresh_fx}
        created = cls.client.post(
            "/forks",
            json={"base_run_id": cls.failed, "edits": [edit], "samples": 4, "control": True},
        )
        assert created.status_code == 202, created.text
        cls.fork_id = created.json()["fork_id"]
        with cls.client.stream("GET", f"/forks/{cls.fork_id}/stream") as stream:
            cls.events = [(event, json.loads(data)) for event, data in _sse(stream.iter_lines())]

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        for key, value in cls.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.directory.cleanup()

    def error(self, response, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)

    def test_step_route_accepts_addresses_with_slashes(self):
        response = self.client.get(f"/runs/{self.failed}/steps/{quote('fx/tool#1', safe='/')}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["addr"], "fx/tool#1")

    def test_unknown_routes_use_the_error_envelope(self):
        self.error(self.client.get("/no-such-route"), 404, "not_found")
        self.error(self.client.delete("/health"), 405, "unsupported")

    def test_cors_allows_the_configured_origin(self):
        response = self.client.get("/health", headers={"Origin": "http://localhost:3000"})
        self.assertEqual(
            response.headers.get("access-control-allow-origin"), "http://localhost:3000"
        )

    def test_malformed_otlp_is_a_bad_request(self):
        response = self.client.post(
            "/v1/traces", content=b"not json", headers={"content-type": "application/json"}
        )
        self.error(response, 400, "bad_request")

    def test_paired_fork_verifies_and_control_is_never_edited(self):
        summary = self.client.get(f"/forks/{self.fork_id}").json()
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["verdict"], "VERIFIED")
        self.assertEqual(self.events[-1][0], "summary")
        control_statuses = {
            data["cache_status"]
            for event, data in self.events
            if event == "step" and data["branch"] == "control" and data["phase"] == "done"
        }
        self.assertNotIn("edited", control_statuses)
        control = self.client.get(f"/runs/{self.fork_id}-control-0").json()
        self.assertNotIn("edited", {step["cache_status"] for step in control["steps"]})
        fixed = self.client.get(f"/runs/{self.fork_id}-fix-0").json()
        edited = {s["addr"] for s in fixed["steps"] if s["cache_status"] == "edited"}
        self.assertEqual(edited, {"fx/tool#1"})

    def test_export_is_keyed_by_fork_and_respects_overwrite(self):
        path = f"/forks/{self.fork_id}/export-test"
        first = self.client.post(path, json={"overwrite": True})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(Path(first.json()["test_path"]).is_file())
        self.error(self.client.post(path, json={"overwrite": False}), 409, "conflict")
        again = self.client.post(path, json={"overwrite": True})
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["fixture_dir"], first.json()["fixture_dir"])
        self.assert_export_replays(first.json())

    def assert_export_replays(self, exported):
        self.assertTrue((Path(exported["fixture_dir"]) / "blackbox.db").is_file())
        result = subprocess.run(
            [sys.executable, exported["test_path"]],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_prompt_fix_export_contains_metadata_and_replays_offline(self):
        created = self.client.post(
            "/tasks/run",
            json={
                "prompt": "Plan a trip from Hyderabad to London departing 2026-12-12, "
                "returning 2026-12-17, for 2 adults. Budget ₹80,000.",
                "inject_stale_fx": True,
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        run_id = created.json()["run_id"]
        self.assertEqual(created.json()["status"], "failed")
        agent = self.service.agent_of(run_id)
        fresh_fx = agent.adapter().oracle_fixes(run_id)["fx/tool#1"]
        fork = self.client.post(
            "/forks",
            json={
                "base_run_id": run_id,
                "samples": 4,
                "control": True,
                "edits": [{"addr": "fx/tool#1", "kind": "patch_tool_result", "value": fresh_fx}],
            },
        )
        self.assertEqual(fork.status_code, 202, fork.text)
        fork_id = fork.json()["fork_id"]
        with self.client.stream("GET", f"/forks/{fork_id}/stream") as stream:
            list(stream.iter_lines())
        exported = self.client.post(f"/forks/{fork_id}/export-test", json={})
        self.assertEqual(exported.status_code, 200, exported.text)
        files = Path(exported.json()["fixture_dir"]) / "prompt-scenarios"
        self.assertEqual(len(list(files.glob("*.json"))), 1)
        self.assert_export_replays(exported.json())

    def test_verification_needs_at_least_four_samples(self):
        response = self.client.post(f"/runs/{self.failed}/verify", json={"samples": 3})
        self.error(response, 422, "validation_error")

    def test_crash_report_includes_verification_fix_and_markdown(self):
        from server import models as m

        mock = json.loads((ROOT / "web/mocks/diagnosis.json").read_text())
        diagnosis = m.Diagnosis.model_validate(mock).model_copy(
            update={
                "run_id": self.failed,
                "verification": self.service.forks.verification_for(self.failed),
            }
        )
        original = self.service.diagnosis
        self.service.diagnosis = lambda run_id, refresh=False: diagnosis
        try:
            report = self.client.get(f"/runs/{self.failed}/report")
            markdown = self.client.get(f"/runs/{self.failed}/report.md")
        finally:
            self.service.diagnosis = original
        self.assertEqual(report.status_code, 200, report.text)
        body = report.json()
        self.assertEqual(diagnosis.verification.verdict, "VERIFIED")
        self.assertEqual(body["verification"]["text"], diagnosis.verification.explanation)
        self.assertIsNotNone(body["recommended_fix"])
        self.assertEqual(markdown.status_code, 200, markdown.text)
        self.assertIn("## Verification", markdown.text)
        self.assertIn("## Recommended fix", markdown.text)

    def test_prompt_runs_work_in_offline_mode(self):
        response = self.client.post(
            "/tasks/run",
            json={
                "prompt": "Plan a trip from Mumbai to Singapore departing 2026-12-12, returning "
                "2026-12-15, for 2 adults. Budget INR 160000.",
                "inject_stale_fx": False,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertGreater(response.json()["steps"], 0)

    def test_same_prompt_prices_the_same_and_old_rate_is_the_only_difference(self):
        prompt = (
            "Plan a trip from Delhi to Tokyo departing 2026-12-12, returning 2026-12-17, "
            "for 2 adults. Budget ₹1,60,000."
        )

        def scenario(stale: bool) -> dict:
            response = self.client.post(
                "/tasks/run", json={"prompt": prompt, "inject_stale_fx": stale}
            )
            self.assertEqual(response.status_code, 201, response.text)
            task_id = self.client.get(f"/runs/{response.json()['run_id']}").json()["run"]["task_id"]
            path = self.service.agents["tripcrew"].data_dir / "prompt-scenarios"
            saved = json.loads((path / f"{task_id}.json").read_text())
            return {"status": response.json()["status"], **saved["scenario"]}

        first, again, stale = scenario(False), scenario(False), scenario(True)
        self.assertEqual(first, again)
        self.assertEqual(first["expected_total_inr"], stale["expected_total_inr"])
        self.assertEqual((first["status"], stale["status"]), ("passed", "failed"))


class EntryPointTests(unittest.TestCase):
    def test_mcp_server_imports(self):
        import blackbox.mcp_server as mcp_server

        self.assertTrue(callable(mcp_server.export_regression_test))


def _sse(lines):
    event = None
    for line in lines:
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: ") and event:
            yield event, line.removeprefix("data: ")
            event = None


if __name__ == "__main__":
    unittest.main()
