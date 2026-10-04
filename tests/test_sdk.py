import asyncio
import tempfile
import unittest

from blackbox.sdk import Recorder
from blackbox.sdk.runtime import Redactor


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def chat(self, **request):
        self.calls += 1
        return {
            "choices": [
                {
                    "message": {
                        "content": "Budget is valid",
                        "reasoning": "Used the exact calculator total",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4, "cached_tokens": 0},
            "request_model": request["model"],
        }


def quote_tool(city):
    return {
        "city": city,
        "quote": {"total_inr": 100},
        "contact": "ops@example.com",
        "debug_key": "gsk_abcdefghijklmnopqrstuvwxyz",
        "budget_label": "₹1,20,000",
    }


def budget_tool(total):
    return {"within_budget": total <= 120, "total": total}


async def record_toy(recorder: Recorder, run_id: str):
    spans = []
    recorder.span_exporter = spans.append
    provider_call_id = f"provider-{run_id}"
    with recorder.run("toy", "trip-1", 7, run_id=run_id) as run:
        with run.step("quote/tool#1", "tool", "quote"):
            quote = await run.tool(quote_tool, city="Mumbai")
            run.state["quote"] = quote["quote"]

        with run.step("budget/tool#1", "tool", "budget"):
            total = run.state["quote"]["total_inr"]
            budget = await run.tool(budget_tool, total=total)
            run.state["budget"] = budget

        with run.step("writer/chat#1", "llm", "writer"):
            budget = run.state["budget"]
            response = await run.chat(
                [
                    {"role": "user", "content": f"Summarize {budget}"},
                    {
                        "role": "assistant",
                        "tool_calls": [{"id": provider_call_id, "type": "function"}],
                    },
                    {"role": "tool", "tool_call_id": provider_call_id, "content": "ok"},
                ],
                model="test-model",
                temperature=0,
            )
            run.state["answer"] = response["choices"][0]["message"]["content"]
        run.set_outcome(True, score=1.0, reason="within budget")
    return spans


class RecorderSDKTests(unittest.TestCase):
    def test_phone_redaction_does_not_corrupt_prompt_ids(self):
        redact = Redactor()
        self.assertEqual(redact("PROMPT-8496625705CA"), "PROMPT-8496625705CA")
        self.assertEqual(redact("Call 849-662-5705 for help"), "Call [REDACTED] for help")

    def test_records_steps_state_provenance_redaction_and_stable_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeLLM()
            recorder = Recorder(directory, mode="live", llm_client=fake)
            try:
                spans = asyncio.run(record_toy(recorder, "run-a"))
                asyncio.run(record_toy(recorder, "run-b"))
                first = recorder.database.query(
                    "SELECT * FROM steps WHERE run_id = 'run-a' ORDER BY seq"
                )
                second = recorder.database.query(
                    "SELECT * FROM steps WHERE run_id = 'run-b' ORDER BY seq"
                )
                edges = recorder.database.query(
                    "SELECT * FROM edges WHERE run_id = 'run-a' ORDER BY edge_id"
                )

                self.assertEqual(len(first), 3)
                self.assertEqual(len(spans), 3)
                self.assertEqual(fake.calls, 2)
                self.assertEqual(
                    [step["request_key"] for step in first],
                    [step["request_key"] for step in second],
                )
                self.assertEqual(
                    [step["state_after"] for step in first],
                    [step["state_after"] for step in second],
                )
                self.assertGreaterEqual(len(edges), 2)
                self.assertTrue(any(edge["kind"] == "state" for edge in edges))
                self.assertTrue(
                    any(
                        edge["src_pointer"] == "/output/quote/total_inr"
                        and edge["dst_pointer"] == "/input/args/total"
                        for edge in edges
                    )
                )

                quote_output = recorder.store.load_json(first[0]["output_hash"])
                self.assertEqual(quote_output["contact"], "[REDACTED]")
                self.assertEqual(quote_output["debug_key"], "[REDACTED]")
                self.assertEqual(quote_output["budget_label"], "₹1,20,000")
                state = recorder.store.load_checkpoint(first[1]["state_after"])
                self.assertEqual(state["quote"], {"total_inr": 100})

                writer_input = recorder.store.load_json(first[2]["input_hash"])
                calls = writer_input["messages"][1]["tool_calls"]
                self.assertEqual(calls[0]["id"], "provider-run-a")
                self.assertEqual(writer_input["messages"][2]["tool_call_id"], "provider-run-a")
            finally:
                recorder.close()

    def test_time_uuid_and_random_are_recorded_values(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(directory, mode="live")

            async def exercise():
                with recorder.run("toy", "values", 3, run_id="values") as run:
                    with run.step("values/state#1", "value", "values"):
                        values = [await run.now(), await run.uuid(), await run.random()]
                    run.set_outcome(True)
                return values

            try:
                values = asyncio.run(exercise())
                self.assertEqual(len(values), 3)
                step = recorder.database.one(
                    "SELECT * FROM steps WHERE run_id = 'values' AND addr = 'values/state#1'"
                )
                self.assertIsNotNone(step)
                self.assertEqual(step["cache_status"], "live")
            finally:
                recorder.close()


if __name__ == "__main__":
    unittest.main()


def catalog_tool(destination):
    return {"currency": "SGD", "nights": 3, "refundable": False, "fare_inr": 24000}


def rate_tool(destination):
    return {"currency": "SGD", "refundable": False, "rate": 64.25}


def total_tool(fare_inr, rate, refundable, currency):
    return {"total": fare_inr + rate}


class InferredProvenanceTests(unittest.TestCase):
    def test_only_distinctive_values_create_inferred_edges(self):
        from blackbox.sdk.runtime import is_informative

        self.assertFalse(is_informative(True))
        self.assertFalse(is_informative(None))
        self.assertFalse(is_informative(3))
        self.assertFalse(is_informative("SGD"))
        self.assertTrue(is_informative(24000))
        self.assertTrue(is_informative(64.25))
        self.assertTrue(is_informative("Singapore"))

        async def scenario(recorder):
            with recorder.run("toy", "trip-2", 7, run_id="inferred") as run:
                with run.step("catalog/tool#1", "tool", "catalog"):
                    catalog = await run.tool(catalog_tool, destination="Singapore")
                with run.step("fx/tool#1", "tool", "fx"):
                    fx = await run.tool(rate_tool, destination="Singapore")
                with run.step("total/tool#1", "tool", "total"):
                    await run.tool(
                        total_tool,
                        fare_inr=catalog["fare_inr"],
                        rate=fx["rate"],
                        refundable=fx["refundable"],
                        currency=fx["currency"],
                    )
                run.set_outcome(True)

        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(directory, mode="offline")
            try:
                asyncio.run(scenario(recorder))
                edges = recorder.database.query(
                    "SELECT src_addr, src_pointer, dst_pointer FROM edges "
                    "WHERE run_id = 'inferred' AND kind = 'inferred' ORDER BY dst_pointer"
                )
            finally:
                recorder.close()
        self.assertEqual(
            [(e["src_addr"], e["src_pointer"], e["dst_pointer"]) for e in edges],
            [
                ("catalog/tool#1", "/output/fare_inr", "/input/args/fare_inr"),
                ("fx/tool#1", "/output/rate", "/input/args/rate"),
            ],
        )
