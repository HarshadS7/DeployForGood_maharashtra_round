"""What the API needs to know about each instrumented agent to present its runs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from blackbox.api.reader import indian_money


@dataclass(frozen=True, slots=True)
class AgentProfile:
    agent_id: str
    display_name: str
    description: str
    final_answer_key: str
    replayable: bool = True

    def task(self, task_id: str, first_input: Any) -> tuple[str, str | None]:
        """A one-line summary for the Runs table and the full task statement."""
        return task_id, None


def _envelope_task(first_input: Any) -> dict[str, Any]:
    """The JSON task envelope the agents send as their first user message."""
    if not isinstance(first_input, dict):
        return {}
    for message in reversed(first_input.get("messages") or []):
        if message.get("role") == "user":
            try:
                envelope = json.loads(message.get("content") or "")
            except ValueError:
                return {}
            task = envelope.get("task") if isinstance(envelope, dict) else None
            return task if isinstance(task, dict) else {}
    return {}


TRIP_REQUEST = re.compile(
    r"travel from (?P<origin>.+?) to (?P<destination>.+?) departing (?P<depart>[\d-]+), "
    r"returning (?P<ret>[\d-]+), for (?P<adults>\d+) adults\. Budget INR "
    # Permit the sentence-final period after a decimal budget (e.g. ``160000.00.``).
    r"(?P<budget>[\d,]+(?:\.\d+)?)(?=$|[^\d])"
)


class TripCrewProfile(AgentProfile):
    def task(self, task_id: str, first_input: Any) -> tuple[str, str | None]:
        task = _envelope_task(first_input)
        request = task.get("request")
        if not isinstance(request, str):
            return task_id, None
        match = TRIP_REQUEST.search(request)
        if match is None:
            short = request if len(request) <= 88 else request[:85].rstrip() + "..."
            return short, task.get("original_prompt") or request
        adults = int(match["adults"])
        summary = (
            f"{match['origin']} → {match['destination']}, {adults} "
            f"{'adult' if adults == 1 else 'adults'}, under "
            f"{indian_money(float(match['budget'].replace(',', '')))}"
        )
        return summary, task.get("original_prompt") or request


PROFILES: dict[str, AgentProfile] = {
    "tripcrew": TripCrewProfile(
        "tripcrew",
        "TripCrew",
        "Multi-agent travel planner: a planner, four parallel scouts (flights, hotels, "
        "weather, FX), a budget calculator, a writer and a verifier.",
        "final_plan",
    ),
}


def profile(agent: str) -> AgentProfile:
    return PROFILES.get(agent) or AgentProfile(
        agent, agent, "An instrumented agent.", "final_answer", replayable=False
    )
