"""The Black Box service: every API and MCP operation, over the recorder directories.

FastAPI (``server/app.py``) and the MCP server (``blackbox/mcp_server.py``) are thin
adapters over this class, so both always return the same contract models.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from blackbox.api import index as idx
from blackbox.api.agents import AgentProfile, profile
from blackbox.api.diagnose import Twin, build_diagnosis
from blackbox.api.errors import ApiError, not_found
from blackbox.api.reader import (
    RunMeta,
    RunReader,
    descendants,
    display_name,
    normalise_statuses,
    provenance,
    reason_class,
)
from blackbox.config import Settings
from blackbox.explain.report import PrecedentLibrary, build_report, is_evaluation_step
from blackbox.forge.adapters import RoutingClient, make_adapter
from blackbox.ml.dataset import _load_trace
from blackbox.sdk import Recorder
from server import models as m

logger = logging.getLogger("blackbox.api")

API_VERSION = m.CONTRACT_VERSION
DETAIL_CACHE = 64


@dataclass
class AgentData:
    """One recorder directory (``data/<agent>``) and what the API needs to serve it."""

    name: str
    data_dir: Path
    recorder: Recorder
    profile: AgentProfile
    reader: RunReader
    replay_args: dict[str, Any]
    _adapter: Any = None
    adapter_error: str | None = None

    @property
    def database(self):
        return self.recorder.database

    @property
    def store(self):
        return self.recorder.store

    def adapter(self) -> Any:
        if self._adapter is None and self.adapter_error is None:
            args = argparse.Namespace(
                agent=self.name,
                stale_fx=False,
                stale_fx_seeds=tuple(self.replay_args.get("stale_fx_seeds", ())),
            )
            try:
                self._adapter = make_adapter(self.recorder, args)
            except (ValueError, OSError) as error:
                self.adapter_error = str(error)
        return self._adapter


@dataclass
class ModelBundle:
    diagnoser: Any
    version: str
    detector: Any | None
    precedents: PrecedentLibrary | None
    trained_at: datetime | None
    dataset_hash: str | None


@dataclass
class _Cache:
    details: OrderedDict[str, m.RunDetail] = field(default_factory=OrderedDict)
    rows: list[dict[str, Any]] | None = None
    passing: dict[str, dict[str, set[str]]] = field(default_factory=dict)


class BlackBoxService:
    def __init__(
        self,
        data_root: Path = Path("data"),
        *,
        settings: Settings | None = None,
        model_dir: Path | None = None,
        eval_dir: Path | None = None,
        static_bundle: bool = False,
    ) -> None:
        self.settings = settings or Settings.load()
        self.mode: m.Mode = self.settings.mode  # type: ignore[assignment]
        self.data_root = Path(data_root)
        self.model_dir = model_dir or self.data_root / "models" / "diagnoser-v1"
        self.eval_dir = eval_dir or self.data_root / "eval"
        self.static_bundle = static_bundle
        self.started_at = datetime.now(UTC)
        self.notes: list[str] = []
        self.lock = threading.RLock()
        self.cache = _Cache()
        self._warm_lock = threading.Lock()
        self._warm_thread: threading.Thread | None = None
        self.agents: dict[str, AgentData] = {}
        self.run_agent: dict[str, str] = {}
        self._open_agents()
        self.model = self._load_model()
        self.ensure_index()
        from blackbox.api.forks import ForkManager

        self.forks = ForkManager(self)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _open_agents(self) -> None:
        if not self.data_root.is_dir():
            self.notes.append(f"No data directory at {self.data_root}.")
            return
        for directory in sorted(self.data_root.iterdir()):
            if not (directory / "blackbox.db").is_file():
                continue
            replay_args: dict[str, Any] = {}
            config = directory / "replay.json"
            if config.is_file():
                replay_args = json.loads(config.read_text(encoding="utf-8"))
            client = RoutingClient(self.settings)
            recorder = Recorder(
                directory, mode=self.settings.mode, settings=self.settings, llm_client=client
            )
            name = directory.name
            self.agents[name] = AgentData(
                name=name,
                data_dir=directory,
                recorder=recorder,
                profile=profile(name),
                reader=RunReader(recorder, idx.FAULT_SPECS),
                replay_args=replay_args,
            )
        self._refresh_run_ids()

    def _refresh_run_ids(self) -> None:
        mapping = {}
        for name, agent in self.agents.items():
            for row in agent.database.query("SELECT run_id FROM runs"):
                mapping[row["run_id"]] = name
        self.run_agent = mapping

    def _load_model(self) -> ModelBundle | None:
        meta_path = self.model_dir / "meta.json"
        if not meta_path.is_file():
            self.notes.append("No trained diagnoser; run `make eval` to enable diagnosis.")
            return None
        try:
            from blackbox.ml.model import Diagnoser

            diagnoser = Diagnoser.load(self.model_dir)
        except Exception as error:  # a missing native library must not take the API down
            self.notes.append(f"Diagnoser could not be loaded: {error}")
            return None
        detector = None
        detector_path = self.model_dir / "detector.txt"
        if detector_path.is_file():
            import lightgbm

            detector = lightgbm.Booster(model_file=str(detector_path))
        precedents = PrecedentLibrary.load(self.model_dir / "precedents.json")
        meta_bytes = meta_path.read_bytes()
        meta = json.loads(meta_bytes)
        trained = meta.get("trained_at")
        # Cached diagnoses and index scores are keyed by this version, so it must change on
        # every retrain; the directory name alone (diagnoser-v1) stays the same.
        name = str(diagnoser.metadata.get("model_version", self.model_dir.name))
        return ModelBundle(
            diagnoser=diagnoser,
            version=f"{name}@{hashlib.sha256(meta_bytes).hexdigest()[:8]}",
            detector=detector,
            precedents=precedents,
            trained_at=datetime.fromisoformat(trained) if trained else None,
            dataset_hash=meta.get("dataset_hash"),
        )

    def close(self) -> None:
        for agent in self.agents.values():
            agent.recorder.close()

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def agent_of(self, run_id: str) -> AgentData:
        name = self.run_agent.get(run_id)
        if name is None:
            self._refresh_run_ids()
            name = self.run_agent.get(run_id)
        if name is None:
            raise not_found("run", run_id, "Check the run id in the Runs list.")
        return self.agents[name]

    def run_row(self, run_id: str) -> dict[str, Any]:
        agent = self.agent_of(run_id)
        row = agent.database.one(
            """
            SELECT r.*, i.task, i.task_text, i.origin, i.split, i.steps AS n_steps,
                   i.duration_ms, i.llm_calls, i.tokens_in, i.tokens_out, i.tokens_cached,
                   i.risk, i.top_addr, i.top_name, i.top_probability, i.failure_signature,
                   l.source AS label_source, l.root_addr AS label_root, l.fault_type,
                   l.recovered AS label_recovered, l.manifest_addr, l.verified AS label_verified
            FROM runs r
            LEFT JOIN run_index i ON i.run_id = r.run_id
            LEFT JOIN labels l ON l.run_id = r.run_id
            WHERE r.run_id = ?
            """,
            (run_id,),
        )
        if row is None:
            raise not_found("run", run_id)
        if row["task"] is None:
            self.ensure_index()
            return self.run_row(run_id)
        row["agent_dir"] = agent.name
        return row

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------

    def ensure_index(self) -> int:
        """Index runs that have no index row yet (new forks, new recordings)."""
        added = 0
        with self.lock:
            for agent in self.agents.values():
                missing = agent.database.query(
                    """
                    SELECT r.* FROM runs r LEFT JOIN run_index i ON i.run_id = r.run_id
                    WHERE i.run_id IS NULL
                    """
                )
                if missing:
                    rows = idx.build_rows(agent.database, agent.store, agent.profile, missing)
                    idx.insert_rows(agent.database, rows)
                    added += len(rows)
            if added:
                self.cache.rows = None
                self._refresh_run_ids()
        return added

    def warm_in_background(self) -> None:
        """Score stale index rows on a daemon thread so startup is not blocked."""
        if self.model is None or self.static_bundle:
            return

        def work() -> None:
            started = time.perf_counter()
            try:
                updated = self.warm()
            except Exception:  # a failed warm-up leaves the index unscored, not the API down
                logger.exception("index warm-up failed")
                return
            if updated:
                logger.info(
                    "Scored %d runs for the index in %.1fs", updated, time.perf_counter() - started
                )

        self._warm_thread = threading.Thread(target=work, name="index-warm", daemon=True)
        self._warm_thread.start()

    def warm(self, batch: int = 200, *, run_ids: list[str] | None = None) -> int:
        """Fill the model-derived index columns (risk, top suspect, signature).

        Only rows scored by another model version (or never scored) are touched; with
        ``run_ids``, only those runs are considered.
        """
        if self.model is None:
            return 0
        with self._warm_lock:
            return self._warm(batch, run_ids)

    def _warm(self, batch: int, run_ids: list[str] | None) -> int:
        from blackbox.ml.model import run_aggregates

        assert self.model is not None
        version = self.model.version
        diagnoser = self.model.diagnoser
        updated = 0
        for agent in self.agents.values():
            sql = """
                SELECT r.* FROM runs r JOIN run_index i ON i.run_id = r.run_id
                WHERE (i.model_version IS NULL OR i.model_version != ?)
                """
            params: tuple[Any, ...] = (version,)
            if run_ids is not None:
                if not run_ids:
                    continue
                sql += f" AND r.run_id IN ({', '.join('?' for _ in run_ids)})"
                params += tuple(run_ids)
            runs = agent.database.query(sql, params)
            for start in range(0, len(runs), batch):
                chunk = runs[start : start + batch]
                traces = [_load_trace(agent.database, agent.store, run) for run in chunk]
                traces = [t for t in traces if t.steps]
                if not traces:
                    continue
                risks: dict[str, float] = {}
                if self.model.detector is not None:
                    matrix = diagnoser.matrix(traces)
                    scores = self.model.detector.predict(run_aggregates(matrix))
                    risks = {run_id: float(s) for run_id, s in zip(matrix.run_ids, scores)}
                failed = [t for t in traces if t.outcome == "failed"]
                tops: dict[str, tuple[str, str, float]] = {}
                for trace, diagnosis in zip(failed, diagnoser.diagnose_many(failed)):
                    roles = {s.addr: s for s in trace.steps}
                    ranking = [
                        (addr, p)
                        for addr, p in diagnosis.ranking
                        if not is_evaluation_step(addr, roles[addr].role)
                    ]
                    if not ranking:
                        continue
                    mass = sum(p for _, p in ranking) or 1.0
                    addr, p = ranking[0]
                    step = roles[addr]
                    name = display_name(
                        {"kind": step.kind, "agent_role": step.role, "name": step.name},
                        step.input,
                    )
                    tops[trace.run_id] = (addr, name, p / mass)
                reasons = {run["run_id"]: run["checker_reason"] for run in chunk}
                rows = []
                for trace in traces:
                    top = tops.get(trace.run_id)
                    signature = (
                        f"{agent.name}:{top[1]}:{reason_class(reasons.get(trace.run_id))}"
                        if top
                        else None
                    )
                    rows.append(
                        (
                            risks.get(trace.run_id),
                            top[0] if top else None,
                            top[1] if top else None,
                            round(top[2], 6) if top else None,
                            signature,
                            version,
                            trace.run_id,
                        )
                    )
                agent.database.executemany(
                    """
                    UPDATE run_index SET risk = ?, top_addr = ?, top_name = ?,
                        top_probability = ?, failure_signature = ?, model_version = ?
                    WHERE run_id = ?
                    """,
                    rows,
                )
                updated += len(rows)
        with self.lock:
            self.cache.rows = None
        return updated

    def _all_rows(self) -> list[dict[str, Any]]:
        with self.lock:
            if self.cache.rows is None:
                rows = []
                for agent in self.agents.values():
                    for row in agent.database.query(
                        """
                        SELECT r.*, i.task, i.task_text, i.origin, i.split, i.steps AS n_steps,
                               i.duration_ms, i.llm_calls, i.tokens_in, i.tokens_out,
                               i.tokens_cached, i.risk, i.top_addr, i.top_name,
                               i.top_probability, i.failure_signature,
                               l.source AS label_source, l.root_addr AS label_root, l.fault_type,
                               l.recovered AS label_recovered, l.manifest_addr,
                               l.verified AS label_verified
                        FROM runs r
                        JOIN run_index i ON i.run_id = r.run_id
                        LEFT JOIN labels l ON l.run_id = r.run_id
                        """
                    ):
                        row["agent_dir"] = agent.name
                        rows.append(row)
                self.cache.rows = rows
            return self.cache.rows

    # ------------------------------------------------------------------
    # Summaries
    # ------------------------------------------------------------------

    def label_info(self, row: dict[str, Any]) -> m.LabelInfo | None:
        if not row.get("label_source"):
            return None
        spec = idx.FAULT_SPECS.get(row["fault_type"])
        return m.LabelInfo(
            source=row["label_source"],
            root_addr=row["label_root"],
            fault_code=spec.code if spec else None,
            fault_type=row["fault_type"],
            fault_family=spec.family if spec else None,
            held_out=spec.held_out if spec else None,
            recovered=bool(row["label_recovered"]),
            manifest_addr=row["manifest_addr"],
            verified=bool(row["label_verified"]),
            confidence="high" if row["label_source"] == "injected" else None,
        )

    def summary(self, row: dict[str, Any], *, blind: bool = False) -> m.RunSummary:
        started = datetime.fromisoformat(row["started_at"])
        ended = datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None
        status = {"passed": "passed", "failed": "failed"}.get(row["outcome"] or "", "running")
        top = None
        if row["top_addr"] and not blind and status == "failed":
            top = m.SuspectBrief(
                addr=row["top_addr"],
                name=row["top_name"],
                probability=min(1.0, max(0.0, row["top_probability"] or 0.0)),
            )
        from blackbox.api.reader import client_kind

        return m.RunSummary(
            run_id=row["run_id"],
            status=status,  # type: ignore[arg-type]
            agent=row["agent"],
            task_id=row["task_id"],
            task=row["task"],
            origin=row["origin"],
            parent_run_id=row["parent_run_id"],
            fork_id=row["fork_id"],
            mode=row["mode"],
            model=row["model"],
            client=client_kind(
                row["model"], row["mode"] if row["agent_dir"] != "imported" else "imported"
            ),
            seed=row["seed"],
            steps=row["n_steps"],
            duration_ms=row["duration_ms"],
            cost=m.Cost(
                llm_calls=row["llm_calls"],
                tokens_in=row["tokens_in"],
                tokens_out=row["tokens_out"],
                tokens_cached=row["tokens_cached"],
                usd=None,
            ),
            started_at=started,
            ended_at=ended,
            score=row["score"],
            checker_reason=row["checker_reason"],
            split=row["split"],
            risk=None if blind or row["risk"] is None else min(1.0, max(0.0, row["risk"])),
            top_suspect=top,
            failure_signature=None if blind else row["failure_signature"],
            label=None if blind else self.label_info(row),
        )

    # ------------------------------------------------------------------
    # Health, agents, runs
    # ------------------------------------------------------------------

    def capabilities(self) -> m.Capabilities:
        writable = not self.static_bundle
        return m.Capabilities(
            diagnose=self.model is not None,
            fork=writable,
            live_calls=self.mode == "live",
            verify=writable and self.model is not None,
            export_test=writable,
            label=writable,
            ingest_otlp=writable,
            eval=(self.eval_dir / "summary.json").is_file(),
        )

    def health(self) -> m.AppHealth:
        rows = self._all_rows()
        counts = {"forks": 0, "labels": 0, "steps": 0}
        for agent in self.agents.values():
            counts["forks"] += agent.database.one("SELECT COUNT(*) AS n FROM forks")["n"]
            counts["labels"] += agent.database.one("SELECT COUNT(*) AS n FROM labels")["n"]
            counts["steps"] += agent.database.one("SELECT COUNT(*) AS n FROM steps")["n"]
        notes = list(self.notes)
        unreplayable = [a.name for a in self.agents.values() if a.adapter() is None]
        if unreplayable:
            notes.append(f"Replay unavailable for: {', '.join(sorted(unreplayable))}.")
        return m.AppHealth(
            status="ok" if self.model is not None and self.agents else "degraded",
            api_version=API_VERSION,
            mode=self.mode,
            static_bundle=self.static_bundle,
            model_version=self.model.version if self.model else None,
            dataset_version=self.dataset_version(),
            capabilities=self.capabilities(),
            counts=m.HealthCounts(
                runs=len(rows),
                steps=counts["steps"],
                forks=counts["forks"],
                labels=counts["labels"],
            ),
            started_at=self.started_at,
            notes=notes,
        )

    def dataset_version(self) -> str | None:
        hashes = []
        for agent in self.agents.values():
            path = agent.data_dir / "frozen" / "DATASET_VERSION"
            if path.is_file():
                hashes.append(path.read_text(encoding="utf-8").strip()[:16])
        return "+".join(hashes) or None

    def list_agents(self) -> m.AgentList:
        rows = [r for r in self._all_rows() if r["origin"] != "fork"]
        items = []
        for name, agent in sorted(self.agents.items()):
            mine = [r for r in rows if r["agent_dir"] == name]
            models = sorted({r["model"] for r in mine if r["model"]})
            steps = [r["n_steps"] for r in mine if r["origin"] == "natural"]
            from blackbox.api.reader import client_kind

            items.append(
                m.AgentInfo(
                    agent_id=name,
                    display_name=agent.profile.display_name,
                    description=agent.profile.description,
                    client=client_kind(
                        models[0] if models else None, "imported" if name == "imported" else None
                    ),
                    models=models,
                    runs=len(mine),
                    passed=sum(r["outcome"] == "passed" for r in mine),
                    failed=sum(r["outcome"] == "failed" for r in mine),
                    typical_steps=Counter(steps).most_common(1)[0][0] if steps else None,
                )
            )
        return m.AgentList(items=items)

    def list_runs(self, query: m.RunListQuery) -> m.RunList:
        rows = self._all_rows()
        text = (query.q or "").strip().lower()

        def keep(row: dict[str, Any]) -> bool:
            if query.origin is None and row["origin"] == "fork":
                return False
            if query.agent and row["agent"] != query.agent:
                return False
            status = row["outcome"] or "running"
            if query.outcome and status != query.outcome:
                return False
            if query.origin and row["origin"] != query.origin:
                return False
            if query.split and row["split"] != query.split:
                return False
            if query.label_source and row["label_source"] != query.label_source:
                return False
            if query.failure_signature and row["failure_signature"] != query.failure_signature:
                return False
            if text and text not in (
                f"{row['run_id']} {row['task_id']} {row['task']} {row['task_text'] or ''}".lower()
            ):
                return False
            return True

        chosen = [row for row in rows if keep(row)]
        key = {
            "started_at": lambda r: r["started_at"],
            "duration_ms": lambda r: r["duration_ms"],
            "risk": lambda r: -1 if r["risk"] is None else r["risk"],
            "steps": lambda r: r["n_steps"],
        }[query.sort]
        chosen.sort(key=lambda r: (key(r), r["run_id"]), reverse=query.order == "desc")
        page = chosen[query.offset : query.offset + query.limit]
        total = len(chosen)
        more = query.offset + query.limit < total
        return m.RunList(
            total=total,
            limit=query.limit,
            offset=query.offset,
            next_offset=query.offset + query.limit if more else None,
            items=[self.summary(row) for row in page],
            facets=m.RunFacets(
                agents=dict(Counter(r["agent"] for r in chosen)),
                outcomes=dict(Counter(r["outcome"] or "running" for r in chosen)),
                origins=dict(Counter(r["origin"] for r in chosen)),
                splits=dict(Counter(r["split"] for r in chosen if r["split"])),
            ),
            fixture=False,
        )

    def failure_groups(
        self, agent: str | None = None, split: str | None = None
    ) -> m.FailureGroupList:
        failed = [
            r
            for r in self._all_rows()
            if r["outcome"] == "failed"
            and r["origin"] != "fork"
            and (agent is None or r["agent"] == agent)
            and (split is None or r["split"] == split)
        ]
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in failed:
            if row["failure_signature"]:
                groups.setdefault(row["failure_signature"], []).append(row)
        items = []
        for signature, members in groups.items():
            members.sort(key=lambda r: r["run_id"])
            faults = Counter(r["fault_type"] for r in members if r["fault_type"])
            _, top_name, kind = signature.split(":", 2)
            items.append(
                m.FailureGroup(
                    signature=signature,
                    label=f"{top_name} · {kind.replace('_', ' ')}",
                    agent=members[0]["agent"],
                    count=len(members),
                    top_suspect_name=top_name,
                    fault_type=faults.most_common(1)[0][0] if faults else None,
                    example_run_ids=[r["run_id"] for r in members[:5]],
                )
            )
        items.sort(key=lambda g: (-g.count, g.signature))
        return m.FailureGroupList(total_failed=len(failed), items=items, fixture=False)

    # ------------------------------------------------------------------
    # Run detail, steps, provenance
    # ------------------------------------------------------------------

    def _fork_statuses(
        self, agent: AgentData, row: dict[str, Any]
    ) -> dict[str, m.CacheStatus] | None:
        if not row["fork_id"]:
            return None
        fork = agent.database.one("SELECT * FROM forks WHERE fork_id = ?", (row["fork_id"],))
        if fork is None:
            return None
        edited = {edit["addr"] for edit in json.loads(fork["edits_json"])}
        base_edges = agent.reader.edges(fork["base_run_id"])
        if f"{row['fork_id']}-control-" in row["run_id"]:
            # A paired control re-runs the cone unchanged: nothing in it was edited.
            invalidated = descendants(base_edges, edited) if edited else set()
            steps = agent.reader.step_details(row["run_id"])
            return normalise_statuses(steps, invalidated, set())
        invalidated = descendants(base_edges, edited) if edited else set()
        if not edited:  # a re-run-unchanged fork: everything it re-ran was invalidated
            invalidated = {
                s["addr"]
                for s in agent.database.query(
                    "SELECT addr FROM steps WHERE run_id = ? AND cache_status = 'live'",
                    (row["run_id"],),
                )
            }
        steps = agent.reader.step_details(row["run_id"])
        return normalise_statuses(steps, invalidated, edited)

    def run_detail(self, run_id: str, *, blind: bool = False) -> m.RunDetail:
        cache_key = f"{run_id}|{int(blind)}"
        with self.lock:
            cached = self.cache.details.get(cache_key)
            if cached is not None:
                self.cache.details.move_to_end(cache_key)
                return cached
        row = self.run_row(run_id)
        agent = self.agents[row["agent_dir"]]
        summary = self.summary(row, blind=blind)
        meta = RunMeta(
            task=row["task"],
            task_text=row["task_text"],
            origin=row["origin"],
            split=row["split"],
            started_at=summary.started_at,
            risk=summary.risk,
            top_suspect=summary.top_suspect,
            failure_signature=summary.failure_signature,
            final_answer_key=agent.profile.final_answer_key,
        )
        detail = agent.reader.detail(
            run_id, meta, status_override=self._fork_statuses(agent, row), fixture=False
        )
        detail = detail.model_copy(update={"run": summary})
        with self.lock:
            self.cache.details[cache_key] = detail
            while len(self.cache.details) > DETAIL_CACHE:
                self.cache.details.popitem(last=False)
        return detail

    def step(self, run_id: str, addr: str) -> m.StepDetail:
        for step in self.run_detail(run_id).steps:
            if step.addr == addr:
                return step
        raise not_found("step", addr, f"Run {run_id} has no step with this address.")

    def provenance(self, run_id: str, addr: str, pointer: str) -> m.ValueProvenance:
        detail = self.run_detail(run_id)
        if addr not in {s.addr for s in detail.steps}:
            raise not_found("step", addr)
        try:
            return provenance(detail, addr, pointer)
        except KeyError as error:
            raise ApiError(
                "validation_error",
                f"{addr} has no value at {pointer!r}.",
                hint="Pointers are relative to the step document: /input, /output, /state_before …",
            ) from error

    # ------------------------------------------------------------------
    # Diagnosis
    # ------------------------------------------------------------------

    def _require_model(self) -> ModelBundle:
        if self.model is None:
            raise ApiError(
                "unavailable",
                "No trained diagnoser is available.",
                hint="Run `make eval` to train one, then restart the API.",
            )
        return self.model

    def passing_layouts(self, agent: AgentData) -> dict[str, set[str]]:
        with self.lock:
            cached = self.cache.passing.get(agent.name)
        if cached is not None:
            return cached
        layouts: dict[str, set[str]] = {}
        for row in agent.database.query(
            """
            SELECT s.run_id, s.addr FROM steps s JOIN runs r ON r.run_id = s.run_id
            WHERE r.outcome = 'passed' AND r.fork_id IS NULL
            """
        ):
            layouts.setdefault(row["run_id"], set()).add(row["addr"])
        with self.lock:
            self.cache.passing[agent.name] = layouts
        return layouts

    def nearest_twin(self, run_id: str) -> Twin | None:
        row = self.run_row(run_id)
        agent = self.agents[row["agent_dir"]]
        layouts = self.passing_layouts(agent)
        mine = {
            s["addr"]
            for s in agent.database.query("SELECT addr FROM steps WHERE run_id = ?", (run_id,))
        }
        tasks = {
            r["run_id"]: r["task_id"]
            for r in agent.database.query(
                "SELECT run_id, task_id FROM runs WHERE outcome = 'passed' AND fork_id IS NULL"
            )
        }
        best: tuple[float, str] | None = None
        for candidate, addrs in layouts.items():
            if candidate == run_id:
                continue
            overlap = len(mine & addrs) / max(len(mine | addrs), 1)
            same = tasks.get(candidate) == row["task_id"]
            parent = candidate == row["parent_run_id"]
            score = overlap + (1.0 if same else 0.0) + (0.5 if parent else 0.0)
            if best is None or score > best[0] or (score == best[0] and candidate < best[1]):
                best = (score, candidate)
        if best is None:
            return None
        twin_id = best[1]
        twin_detail = self.run_detail(twin_id)
        same_task = tasks.get(twin_id) == row["task_id"]
        overlap = len(mine & layouts[twin_id]) / max(len(mine | layouts[twin_id]), 1)
        return Twin(
            summary=twin_detail.run,
            steps={s.addr: s for s in twin_detail.steps},
            similarity=overlap,
            same_task=same_task,
            basis=["agent", "graph_shape"],
        )

    def twin(self, run_id: str) -> m.NearestTwin:
        diagnosis = self.diagnosis(run_id)
        if diagnosis.nearest_twin is None:
            raise not_found("passing twin for run", run_id, "No passing run of this agent exists.")
        return diagnosis.nearest_twin

    def _oracle(self, agent: AgentData, run_id: str) -> dict[str, Any]:
        adapter = agent.adapter()
        if adapter is None:
            return {}
        try:
            return adapter.oracle_fixes(run_id)
        except Exception as error:  # oracle values are optional evidence
            logger.debug("oracle fixes unavailable for %s: %s", run_id, error)
            return {}

    def diagnosis(self, run_id: str, *, refresh: bool = False) -> m.Diagnosis:
        bundle = self._require_model()
        row = self.run_row(run_id)
        if row["outcome"] != "failed":
            raise ApiError(
                "not_applicable",
                "This run passed, so there is no failure to diagnose.",
                hint="Open a failed run, or compare this run with a failed one.",
            )
        agent = self.agents[row["agent_dir"]]
        verification = self.forks.verification_for(run_id)
        if not refresh:
            stored = agent.database.one(
                "SELECT evidence_json FROM diagnoses WHERE run_id = ? AND model_version = ?",
                (run_id, bundle.version),
            )
            if stored is not None:
                diagnosis = m.Diagnosis.model_validate_json(stored["evidence_json"])
                return self._with_verification(diagnosis, verification)
        started = time.perf_counter()
        trace = _load_trace(agent.database, agent.store, row)
        report = build_report(bundle.diagnoser, trace, precedents=bundle.precedents)
        latency = (time.perf_counter() - started) * 1000
        detail = self.run_detail(run_id)
        diagnosis = build_diagnosis(
            diagnoser=bundle.diagnoser,
            report=report,
            detail=detail,
            latency_ms=latency,
            twin=self.nearest_twin(run_id),
            oracle=self._oracle(agent, run_id),
            verification=None,
            label_source=self._label_source,
        )
        agent.database.execute(
            """
            INSERT OR REPLACE INTO diagnoses(
                run_id, model_version, ranking_json, conformal_set_json, abstain,
                evidence_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                bundle.version,
                json.dumps([[s.addr, s.probability] for s in diagnosis.ranking]),
                json.dumps(diagnosis.conformal_set),
                int(diagnosis.abstain),
                diagnosis.model_dump_json(),
                datetime.now(UTC).isoformat(),
            ),
        )
        return self._with_verification(diagnosis, verification)

    def _label_source(self, run_id: str) -> str:
        try:
            row = self.run_row(run_id)
        except ApiError:
            return "injected"
        return row["label_source"] or "injected"

    @staticmethod
    def _with_verification(
        diagnosis: m.Diagnosis, verification: m.Verification | None
    ) -> m.Diagnosis:
        from blackbox.api.diagnose import stage_rail

        if verification is None:
            return diagnosis
        return diagnosis.model_copy(
            update={
                "verification": verification,
                "stages": stage_rail(
                    diagnosis.abstain, diagnosis.reasons, diagnosis.proposed_fixes, verification
                ),
            }
        )
