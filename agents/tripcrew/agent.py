"""Sixteen recorded steps with concurrent, independently scoped travel workers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from agents.tripcrew.checker import check_plan
from agents.tripcrew.mock_apis import TravelAPI, calculate_budget
from agents.tripcrew.models import (
    Constraints,
    FlightChoice,
    FlightQuery,
    HotelChoice,
    HotelQuery,
    Plan,
)
from agents.tripcrew.prompts import messages
from agents.tripcrew.scenarios import Scenario
from blackbox.sdk import RunSession


@dataclass
class Job:
    addr: str
    kind: str
    key: str
    action: Callable[[], Awaitable[Any]]


async def _wave(run: RunSession, jobs: list[Job]):
    """Calls run concurrently; scope entry and state commits retain a stable order.

    Every scope starts before any result is committed. This keeps prefix snapshot
    hashes stable even when live calls finish in a different order than cache hits.
    No worker mutates shared State until the wave's calls have all finished.
    """
    started = [asyncio.Event() for _ in range(len(jobs) + 1)]
    commit = [asyncio.Event() for _ in range(len(jobs) + 1)]
    started[0].set()
    commit[0].set()
    ready = asyncio.Barrier(len(jobs))
    finished = asyncio.Barrier(len(jobs))

    async def worker(index, job):
        await started[index].wait()
        with run.step(job.addr, job.kind, agent_role=job.addr.split("/")[0]):
            started[index + 1].set()
            await ready.wait()
            error = None
            result = None
            try:
                result = await job.action()
            except Exception as exc:
                error = exc
            await finished.wait()
            await commit[index].wait()
            try:
                if error is not None:
                    raise error
                run.state[job.key] = result
            finally:
                commit[index + 1].set()

    tasks = [asyncio.create_task(worker(i, job)) for i, job in enumerate(jobs)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class TripCrew:
    def __init__(
        self, scenario: Scenario, api: TravelAPI, *, model=None, task_prompt: str | None = None
    ):
        self.scenario = scenario
        self.api = api
        self.model = model
        self.task_prompt = task_prompt

    async def __call__(self, run: RunSession):
        scenario = self.scenario

        async def chat(role, payload, schema):
            response = await run.chat(
                messages(role, payload, schema),
                model=self.model,
                response_format={"type": "json_object"},
                temperature=0.2,
                seed=run.seed,
                max_tokens=1200,
            )
            try:
                content = response["choices"][0]["message"]["content"]
                return schema.model_validate(json.loads(content)).model_dump()
            except (KeyError, IndexError, TypeError, ValueError) as error:
                raise ValueError(f"{role} returned invalid structured output: {error}") from error

        with run.step("planner/chat#1", "llm", agent_role="planner"):
            request = {"request": scenario.request}
            if self.task_prompt:
                request["original_prompt"] = self.task_prompt
            run.state["constraints"] = await chat("planner", request, Constraints)
        with run.step("planner/state#1", "state", agent_role="planner"):
            constraints = run.state["constraints"]
            for key, schema in (("flight_task", FlightQuery), ("hotel_task", HotelQuery)):
                run.state[key] = {name: constraints[name] for name in schema.model_fields}
            run.state["destination"] = constraints["destination"]

        async def flight_query():
            return await chat("flight_query", run.state["flight_task"], FlightQuery)

        async def hotel_query():
            return await chat("hotel_query", run.state["hotel_task"], HotelQuery)

        async def observation(function):
            return await run.tool(
                function, scenario_id=scenario.scenario_id, destination=run.state["destination"]
            )

        await _wave(
            run,
            [
                Job("flight/chat#1", "llm", "flight_query", flight_query),
                Job("hotel/chat#1", "llm", "hotel_query", hotel_query),
                Job("weather/tool#1", "tool", "weather", lambda: observation(self.api.get_weather)),
                Job("fx/tool#1", "tool", "fx", lambda: observation(self.api.fx_rate)),
                Job("visa/tool#1", "tool", "visa", lambda: observation(self.api.visa_rules)),
            ],
        )

        async def flights():
            return await run.tool(
                self.api.search_flights,
                scenario_id=scenario.scenario_id,
                **run.state["flight_query"],
            )

        async def hotels():
            return await run.tool(
                self.api.search_hotels, scenario_id=scenario.scenario_id, **run.state["hotel_query"]
            )

        await _wave(
            run,
            [
                Job("flight/tool#1", "tool", "flight_catalog", flights),
                Job("hotel/tool#1", "tool", "hotel_catalog", hotels),
            ],
        )

        async def select(branch, schema):
            catalog = run.state[f"{branch}_catalog"]
            choice = await chat(
                f"{branch}_select",
                {"query": run.state[f"{branch}_query"], "catalog": catalog},
                schema,
            )
            chosen_id = choice[f"{branch}_id"]
            options = [item for item in catalog["options"] if item["id"] == chosen_id]
            if not options:
                raise ValueError(f"{branch} selected an unknown ID: {chosen_id}")
            return options[0]

        await _wave(
            run,
            [
                Job("flight/chat#2", "llm", "flight", lambda: select("flight", FlightChoice)),
                Job("hotel/chat#2", "llm", "hotel", lambda: select("hotel", HotelChoice)),
            ],
        )
        with run.step("budget/tool#1", "tool", agent_role="budget"):
            run.state["budget"] = await run.tool(
                calculate_budget,
                flight=run.state["flight"],
                hotel=run.state["hotel"],
                fx=run.state["fx"],
                visa=run.state["visa"],
                adults=run.state["constraints"]["adults"],
                nights=run.state["hotel_catalog"]["nights"],
                rooms=run.state["hotel_catalog"]["rooms"],
            )
        with run.step("writer/chat#1", "llm", agent_role="writer"):
            run.state["draft"] = await chat(
                "writer",
                {
                    "constraints": run.state["constraints"],
                    "flight": run.state["flight"],
                    "hotel": run.state["hotel"],
                    "budget": run.state["budget"],
                    "weather": run.state["weather"],
                    "visa": run.state["visa"],
                },
                Plan,
            )
        with run.step("verifier/chat#1", "llm", agent_role="verifier"):
            run.state["verified_plan"] = await chat(
                "verifier",
                {
                    "plan": run.state["draft"],
                    "constraints": run.state["constraints"],
                    "budget": run.state["budget"],
                    "flight": run.state["flight"],
                    "hotel": run.state["hotel"],
                },
                Plan,
            )
        with run.step("final/state#1", "state", agent_role="final"):
            run.state["final_plan"] = run.state["verified_plan"]
        with run.step("checker/state#1", "state", agent_role="checker"):
            check = check_plan(scenario, run.state["final_plan"])
            run.state["check"] = asdict(check)
        run.set_outcome(check.passed, score=float(check.passed), reason=check.reason)
        return run.state.as_dict()["final_plan"]
