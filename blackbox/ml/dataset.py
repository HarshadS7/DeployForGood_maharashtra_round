"""Load recorded runs as label-free traces, with labels kept in a separate structure.

A :class:`Trace` holds only what a real failed run would have: its steps, their exact
payloads and the provenance edges. Labels (root step, fault type, distractor) live in
:class:`RootLabel` objects that feature code never receives, so "the model cannot see
the answer" is a property of the types rather than of reviewer discipline.

Replay bookkeeping (``cache_status``, ``latency_ms``) is deliberately not loaded: in a
fork the injected step is marked ``edited`` and the cone runs live, so those columns
point straight at the answer and do not exist for real (non-replayed) failures.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blackbox.forge.operators import all_operators
from blackbox.recorder import Store
from blackbox.store import SQLiteDatabase

FAULT_HELD_OUT = {op.spec.name: op.spec.held_out for op in all_operators()}
FAULT_CODE = {op.spec.name: op.spec.code for op in all_operators()}


@dataclass(slots=True)
class TraceStep:
    addr: str
    seq: int
    kind: str
    name: str
    role: str | None
    input: Any
    output: Any
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    finish_reason: str | None
    error_type: str | None
    retries: int


@dataclass(slots=True)
class Trace:
    """One run, as the diagnoser is allowed to see it."""

    run_id: str
    agent: str
    task_id: str
    outcome: str | None
    steps: list[TraceStep]
    # (src_addr, dst_addr, kind) with kind in {"state", "message", "inferred"}
    edges: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def addrs(self) -> list[str]:
        return [step.addr for step in self.steps]


@dataclass(frozen=True, slots=True)
class RootLabel:
    """Ground truth for one failed run. Never passed to feature code."""

    run_id: str
    root_addr: str
    fault_type: str
    source: str  # injected | natural_auto | human | verified
    held_out: bool
    distractor_addr: str | None
    base_run_id: str | None


@dataclass(slots=True)
class Corpus:
    traces: dict[str, Trace]
    labels: dict[str, RootLabel]
    passing: list[str]  # passing base runs: the unlabeled "healthy" reference
    recovered: list[str]  # forks that absorbed their fault: hard negatives
    dataset_hash: str | None = None

    def failed(self, *, source: str | None = None) -> list[str]:
        return [
            run_id
            for run_id, label in self.labels.items()
            if source is None or label.source == source
        ]


def _json(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


def _keys(versions: Iterable[str]) -> list[str]:
    return [item.rsplit("@v", 1)[0] for item in versions]


def _load_blob(store: Store, digest: str | None) -> Any:
    if not digest:
        return None
    try:
        return store.load_json(digest)
    except (OSError, ValueError):
        return None


def _state_output(store: Store, row: dict[str, Any], writes: list[str]) -> Any:
    """A state step makes no call, so its "output" is the values it wrote."""
    if not row.get("state_after") or not writes:
        return None
    try:
        state = store.load_checkpoint(row["state_after"])
    except (OSError, ValueError, KeyError):
        return None
    return {key: state.get(key) for key in _keys(writes)}


def _load_trace(database: SQLiteDatabase, store: Store, run: dict[str, Any]) -> Trace:
    steps = []
    for row in database.query(
        "SELECT * FROM steps WHERE run_id = ? ORDER BY seq", (run["run_id"],)
    ):
        writes = _json(row["writes_json"], [])
        output = _load_blob(store, row["output_hash"])
        if output is None and row["kind"] == "state":
            output = _state_output(store, row, writes)
        steps.append(
            TraceStep(
                addr=row["addr"],
                seq=row["seq"],
                kind=row["kind"],
                name=row["name"],
                role=row["agent_role"],
                input=_load_blob(store, row["input_hash"]),
                output=output,
                reads=tuple(_json(row["reads_json"], [])),
                writes=tuple(writes),
                finish_reason=row["finish_reason"],
                error_type=row["error_type"],
                retries=row["retries"] or 0,
            )
        )
    edges = sorted(
        {
            (edge["src_addr"], edge["dst_addr"], edge["kind"])
            for edge in database.query(
                "SELECT src_addr, dst_addr, kind FROM edges WHERE run_id = ?", (run["run_id"],)
            )
            if edge["src_addr"] != edge["dst_addr"]
        }
    )
    return Trace(
        run_id=run["run_id"],
        agent=run["agent"],
        task_id=run["task_id"],
        outcome=run["outcome"],
        steps=steps,
        edges=edges,
    )


def load_trace(database: SQLiteDatabase, store: Store, run_id: str) -> Trace:
    """One recorded run as a label-free trace."""
    run = database.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    if run is None:
        raise KeyError(f"unknown run: {run_id}")
    return _load_trace(database, store, run)


def _distractor(edits_json: str | None, root_addr: str) -> str | None:
    for edit in _json(edits_json, []):
        if edit.get("addr") != root_addr:
            return edit.get("addr")
    return None


def load_corpus(data_dirs: Iterable[str | Path]) -> Corpus:
    """Load every recorder directory (e.g. ``data/tripcrew``).

    Each fork contributes one example: K replay samples of a fork are copies of the
    same experiment, and counting them separately would inflate N and could split a
    single example across train and test.
    """
    traces: dict[str, Trace] = {}
    labels: dict[str, RootLabel] = {}
    passing: list[str] = []
    recovered: list[str] = []
    hashes = []
    for directory in data_dirs:
        directory = Path(directory)
        database_path = directory / "blackbox.db"
        if not database_path.exists():
            continue
        version = directory / "frozen" / "DATASET_VERSION"
        if version.exists():
            hashes.append(version.read_text(encoding="utf-8").strip())
        store = Store(directory / "content")
        database = SQLiteDatabase(database_path)
        try:
            rows = database.query(
                """
                SELECT l.*, r.fork_id, r.agent, r.task_id, r.outcome, r.parent_run_id,
                       f.edits_json, f.base_run_id
                FROM labels l
                JOIN runs r ON r.run_id = l.run_id
                LEFT JOIN forks f ON f.fork_id = r.fork_id
                ORDER BY l.run_id
                """
            )
            seen_forks: set[str] = set()
            chosen: list[dict[str, Any]] = []
            for row in rows:
                if row["fork_id"]:
                    if row["fork_id"] in seen_forks:
                        continue
                    seen_forks.add(row["fork_id"])
                chosen.append(row)
            for row in chosen:
                run = database.one("SELECT * FROM runs WHERE run_id = ?", (row["run_id"],))
                if row["recovered"]:
                    traces[row["run_id"]] = _load_trace(database, store, run)
                    recovered.append(row["run_id"])
                    continue
                traces[row["run_id"]] = _load_trace(database, store, run)
                labels[row["run_id"]] = RootLabel(
                    run_id=row["run_id"],
                    root_addr=row["root_addr"],
                    fault_type=row["fault_type"] or "unknown",
                    source=row["source"],
                    held_out=FAULT_HELD_OUT.get(row["fault_type"], False),
                    distractor_addr=_distractor(row["edits_json"], row["root_addr"]),
                    base_run_id=row["base_run_id"] or row["parent_run_id"],
                )
            for run in database.query(
                "SELECT * FROM runs WHERE outcome = 'passed' AND fork_id IS NULL ORDER BY run_id"
            ):
                traces[run["run_id"]] = _load_trace(database, store, run)
                passing.append(run["run_id"])
        finally:
            database.close()
    return Corpus(
        traces=traces,
        labels=labels,
        passing=passing,
        recovered=recovered,
        dataset_hash="+".join(hashes) or None,
    )


def graded_relevance(trace: Trace, label: RootLabel) -> dict[str, int]:
    """Root = 3, one causal hop = 2, two hops = 1, everything else (distractors too) = 0.

    Hops are counted on recorded state/message provenance in either direction, so a
    step that fed the root or consumed its output gets partial credit; ranking it
    second is a near miss, not a total miss.
    """
    adjacency: dict[str, set[str]] = defaultdict(set)
    for src, dst, kind in trace.edges:
        if kind in {"state", "message"}:
            adjacency[src].add(dst)
            adjacency[dst].add(src)
    relevance = {addr: 0 for addr in trace.addrs}
    if label.root_addr not in relevance:
        return relevance
    frontier = {label.root_addr}
    seen = {label.root_addr}
    for grade in (3, 2, 1):
        for addr in frontier:
            relevance[addr] = max(relevance[addr], grade)
        frontier = {n for addr in frontier for n in adjacency[addr]} - seen
        seen |= frontier
    if label.distractor_addr in relevance:
        relevance[label.distractor_addr] = 0
    return relevance
