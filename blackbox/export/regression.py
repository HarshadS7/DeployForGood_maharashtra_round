"""Export a VERIFIED fork as an offline regression test (Task 9).

``export`` writes ``tests/regressions/test_<run>_<addr>.py`` and a minimal fixture
directory next to it: the failed run's steps, edges, cassette entries and content blobs,
plus the cassette entries the verified fix needs, so the test replays with
``MODE=recorded`` and no network. The generated test asserts that

1. the original fixture still fails;
2. the saved patch makes it pass;
3. every step before the edit reaches the recorded state hash (``ReplayDivergence``
   otherwise), and the edited step starts from the recorded state;
4. the static invalidation cone equals the saved cone.

Deleting the ``PATCH`` entries makes assertion 2 fail, which is the point of the test.
A ``rerun`` fix cannot be replayed offline, so it is frozen into the concrete output the
verified replay observed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import socket
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from blackbox.recorder import Store, content_hash
from blackbox.replay import Edit, ReplayEngine
from blackbox.sdk import Recorder
from blackbox.store import SQLiteDatabase

FIXTURE_FORMAT = "blackbox-regression-v1"


class ExportError(ValueError):
    pass


def _slug(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", value).strip("_").lower()


def _columns(database: SQLiteDatabase, table: str) -> list[str]:
    return [row["name"] for row in database.query(f"PRAGMA table_info({table})")]


def _insert(database: SQLiteDatabase, table: str, rows: list[dict[str, Any]], skip=()) -> None:
    if not rows:
        return
    columns = [c for c in _columns(database, table) if c not in skip]
    marks = ",".join("?" * len(columns))
    database.executemany(
        f"INSERT OR IGNORE INTO {table}({','.join(columns)}) VALUES ({marks})",
        [[row.get(c) for c in columns] for row in rows],
    )


def _copy_blob(source: Store, target: Store, kind: str, ref: str | None) -> None:
    if not ref:
        return
    path = source.root / kind / f"{ref}.json"
    if path.exists():
        shutil.copyfile(path, target.root / kind / f"{ref}.json")


def _frozen_edits(recorder: Recorder, fork: dict[str, Any], fix_run: str) -> list[dict[str, Any]]:
    edits = json.loads(fork["edits_json"])
    frozen = []
    for edit in edits:
        if edit["kind"] == "ghost_hint":
            raise ExportError("ghost-hint forks hide their edit and cannot be exported")
        if edit["kind"] == "rerun":
            step = recorder.database.one(
                "SELECT output_hash FROM steps WHERE run_id = ? AND addr = ?",
                (fix_run, edit["addr"]),
            )
            if not step or not step["output_hash"]:
                raise ExportError(f"verified rerun output of {edit['addr']} is unavailable")
            edit = {
                **edit,
                "kind": "override_output",
                "value": recorder.store.load_json(step["output_hash"]),
            }
        frozen.append(edit)
    return frozen


@dataclass(slots=True)
class Export:
    test_path: Path
    fixture_dir: Path
    export_id: str


def export(
    recorder: Recorder,
    fork_id: str,
    out_dir: Path = Path("tests/regressions"),
    *,
    adapter_args: dict[str, Any] | None = None,
    dataset_hash: str | None = None,
    model_version: str | None = None,
) -> Export:
    """Write the regression test and fixture for one VERIFIED fork."""
    database = recorder.database
    fork = database.one("SELECT * FROM forks WHERE fork_id = ?", (fork_id,))
    if fork is None:
        raise ExportError(f"unknown fork: {fork_id}")
    if fork["verdict"] != "VERIFIED":
        raise ExportError(f"fork {fork_id} is {fork['verdict'] or 'unverified'}, not VERIFIED")
    run_id = fork["base_run_id"]
    run = database.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    crashed = database.one(
        "SELECT addr FROM steps WHERE run_id = ? AND request_key IS NULL "
        "AND error_type IS NOT NULL AND kind = 'llm'",
        (run_id,),
    )
    if crashed is not None:
        raise ExportError(
            f"{crashed['addr']} crashed inside its model call when {run_id} was recorded, so "
            "no offline replay can reproduce the failure (a crashed tool call re-runs locally, "
            "a model call cannot); re-record the run first"
        )
    passing = database.one(
        "SELECT run_id FROM runs WHERE fork_id = ? AND run_id LIKE ? AND outcome = 'passed' "
        "ORDER BY run_id LIMIT 1",
        (fork_id, f"{fork_id}-fix-%"),
    )
    if passing is None:
        raise ExportError("the verified fork has no passing edited sample")
    fix_run = passing["run_id"]
    edits = _frozen_edits(recorder, fork, fix_run)
    if not edits:
        raise ExportError("a regression export needs an intervention to replay")
    addr = edits[0]["addr"]

    stem = f"test_{_slug(run_id)[:24]}_{_slug(addr)}"
    fixture_dir = out_dir / "fixtures" / stem
    if fixture_dir.exists():
        shutil.rmtree(fixture_dir)
    fixture_dir.mkdir(parents=True)
    if run["task_id"].startswith("PROMPT-"):
        metadata = recorder.data_dir / "prompt-scenarios" / f"{run['task_id']}.json"
        if not metadata.is_file():
            raise ExportError("the prompt's replay metadata is unavailable")
        target_metadata = fixture_dir / "prompt-scenarios" / metadata.name
        target_metadata.parent.mkdir()
        shutil.copyfile(metadata, target_metadata)
    target_store = Store(fixture_dir / "content")
    target = SQLiteDatabase(fixture_dir / "blackbox.db")
    try:
        steps = database.query("SELECT * FROM steps WHERE run_id = ? ORDER BY seq", (run_id,))
        fix_steps = database.query("SELECT request_key FROM steps WHERE run_id = ?", (fix_run,))
        # The failed run becomes a root run of the fixture: no parent, no fork.
        _insert(target, "runs", [{**run, "parent_run_id": None, "fork_id": None}])
        _insert(target, "steps", steps, skip={"step_id"})
        _insert(
            target,
            "edges",
            database.query("SELECT * FROM edges WHERE run_id = ?", (run_id,)),
            skip={"edge_id"},
        )
        keys = sorted({s["request_key"] for s in steps + fix_steps if s["request_key"]})
        cassette = []
        for start in range(0, len(keys), 500):
            chunk = keys[start : start + 500]
            cassette += database.query(
                f"SELECT * FROM cassette WHERE request_key IN ({','.join('?' * len(chunk))})",
                chunk,
            )
        _insert(target, "cassette", cassette)
        for step in steps:
            for column in ("input_hash", "output_hash", "reasoning_hash"):
                _copy_blob(recorder.store, target_store, "blobs", step[column])
            for column in ("state_before", "state_after"):
                ref = step[column]
                if not ref:
                    continue
                _copy_blob(recorder.store, target_store, "checkpoints", ref)
                for value_ref in recorder.store.checkpoint_entries(ref).values():
                    _copy_blob(recorder.store, target_store, "blobs", value_ref)
        for row in cassette:
            _copy_blob(recorder.store, target_store, "blobs", row["response_hash"])
    finally:
        target.close()

    cone = sorted(ReplayEngine(recorder)._descendants(run_id, {e["addr"] for e in edits}))
    manifest = {
        "format": FIXTURE_FORMAT,
        "run_id": run_id,
        "fork_id": fork_id,
        "agent": run["agent"],
        "task_id": run["task_id"],
        "edits": edits,
        "cone": cone,
        "edit_state_before": next(s["state_before"] for s in steps if s["addr"] == addr),
        "adapter_args": {"agent": run["agent"], "stale_fx": False, **(adapter_args or {})},
        "verified": {
            "samples": fork["samples"],
            "fix_rate": fork["fix_pass_rate"],
            "fix_ci": [fork["fix_ci_low"], fork["fix_ci_high"]],
            "control_rate": fork["control_pass_rate"],
            "control_ci": [fork["control_ci_low"], fork["control_ci_high"]],
        },
        "dataset_hash": dataset_hash,
        "model_version": model_version,
        "exported_at": datetime.now(UTC).isoformat(),
    }
    (fixture_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8"
    )
    test_path = out_dir / f"{stem}.py"
    test_path.write_text(_render(stem, manifest), encoding="utf-8")
    init = out_dir / "__init__.py"
    if not init.exists():
        init.write_text('"""Regression tests exported from VERIFIED forks."""\n', encoding="utf-8")

    export_id = uuid.uuid4().hex
    database.execute(
        "INSERT INTO regression_exports(export_id, fork_id, test_hash, fixture_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            export_id,
            fork_id,
            content_hash(test_path.read_text(encoding="utf-8")),
            content_hash(manifest),
            manifest["exported_at"],
        ),
    )
    return Export(test_path=test_path, fixture_dir=fixture_dir, export_id=export_id)


def _describe(edit: dict[str, Any]) -> str:
    value = json.dumps(edit.get("value"), default=str)
    if len(value) > 160:
        value = value[:157] + "..."
    return f"{edit['kind']} at {edit['addr']}: {value}"


def _render(stem: str, manifest: dict[str, Any]) -> str:
    verified = manifest["verified"]
    patch_lines = "\n".join(f"#   {_describe(edit)}" for edit in manifest["edits"])
    return f'''"""Regression test exported by Black Box from a VERIFIED fork.

Source run:  {manifest["run_id"]}
Source fork: {manifest["fork_id"]}
Agent/task:  {manifest["agent"]} / {manifest["task_id"]}
Verified:    fix {verified["fix_rate"]:.0%} of {verified["samples"]} vs control {verified["control_rate"]:.0%}
Dataset:     {manifest["dataset_hash"]}
Model:       {manifest["model_version"]}

Runs offline (MODE=recorded, sockets blocked). Remove PATCH to watch it fail.
"""

import json
import unittest
from pathlib import Path

from blackbox.export.regression import replay_fixture

FIXTURE = Path(__file__).parent / "fixtures" / "{stem}"
MANIFEST = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))

# The fix, in human-readable form:
{patch_lines}
PATCH = MANIFEST["edits"]


class Regression(unittest.TestCase):
    def test_patch_repairs_the_recorded_failure(self):
        result = replay_fixture(FIXTURE, PATCH)
        self.assertEqual(result["original_outcome"], "failed", "the recorded failure no longer reproduces")
        self.assertEqual(result["patched_outcome"], "passed", result["patched_reason"])
        self.assertTrue(result["edit_state_matches"], "state before the edit differs from the recording")
        self.assertEqual(result["cone"], MANIFEST["cone"])


if __name__ == "__main__":
    unittest.main()
'''


@contextmanager
def no_network() -> Iterator[None]:
    """Refuse every non-loopback connection for the duration.

    Loopback stays open because the event loop itself needs it (asyncio's self-pipe is
    a loopback socket pair on Windows); nothing on loopback reaches another machine.
    """
    saved = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection)

    def blocked(address: Any) -> bool:
        host = address[0] if isinstance(address, tuple) else None
        return host not in {"127.0.0.1", "::1", "localhost"}

    def connect(self: socket.socket, address: Any) -> Any:
        if blocked(address):
            raise OSError("network access is disabled in regression tests")
        return saved[0](self, address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        if blocked(address):
            raise OSError("network access is disabled in regression tests")
        return saved[1](self, address)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        if blocked(address):
            raise OSError("network access is disabled in regression tests")
        return saved[2](address, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.create_connection = create_connection  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection = saved


def replay_fixture(fixture: Path, patch: list[dict[str, Any]]) -> dict[str, Any]:
    """Replay a fixture copy without and with ``patch``; report what the test asserts."""
    from blackbox.config import Settings
    from blackbox.forge.__main__ import make_adapter

    manifest = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as directory, no_network():
        workdir = Path(directory) / "fixture"
        shutil.copytree(fixture, workdir)
        settings = Settings.load(env_path=workdir / "missing.env", environ={"MODE": "recorded"})
        recorder = Recorder(workdir, mode="recorded", settings=settings, llm_client=None)
        try:
            adapter = make_adapter(
                recorder,
                argparse.Namespace(
                    **{
                        "stale_fx": False,
                        **manifest["adapter_args"],
                    }
                ),
            )
            agent_fn = adapter.factory(manifest["run_id"])
            engine = ReplayEngine(recorder)
            run_id = manifest["run_id"]
            original = asyncio.run(engine.replay(run_id, agent_fn, edits=[], samples=1))
            edits = [
                Edit(e["addr"], e["kind"], e["value"], e.get("known_good", False)) for e in patch
            ]
            patched = asyncio.run(engine.replay(run_id, agent_fn, edits=edits, samples=1))
            edit_state = None
            if edits:
                row = recorder.database.one(
                    "SELECT state_before FROM steps WHERE run_id = ? AND addr = ?",
                    (patched.edited[0].run_id, edits[0].addr),
                )
                edit_state = row["state_before"] if row else None
            return {
                "original_outcome": original.edited[0].outcome,
                "patched_outcome": patched.edited[0].outcome,
                "patched_reason": patched.edited[0].reason,
                "edit_state_matches": edit_state == manifest["edit_state_before"],
                "cone": sorted(patched.invalidated),
            }
        finally:
            recorder.close()
