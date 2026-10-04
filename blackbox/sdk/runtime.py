"""Execution recorder used by agent code and the replay engine."""

from __future__ import annotations

import contextvars
import inspect
import json
import random as random_module
import re
import time
import uuid as uuid_module
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from blackbox.config import Settings
from blackbox.llm import AsyncLLMClient
from blackbox.recorder import Store, content_hash
from blackbox.sdk.state import State
from blackbox.store import SQLiteDatabase

CacheStatus = Literal["cached", "invalidated", "live", "edited"]

_current_run: contextvars.ContextVar[RunSession | None] = contextvars.ContextVar(
    "blackbox_current_run", default=None
)
_current_step: contextvars.ContextVar[StepScope | None] = contextvars.ContextVar(
    "blackbox_current_step", default=None
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def iter_scalar_pointers(value: Any, pointer: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from iter_scalar_pointers(item, f"{pointer}/{_escape_pointer(str(key))}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from iter_scalar_pointers(item, f"{pointer}/{index}")
    else:
        yield pointer or "/", value


def is_informative(value: Any) -> bool:
    """Whether a scalar is distinctive enough to infer provenance from value equality.

    Booleans, nulls, small integers and very short strings (``true``, ``4``, ``"INR"``)
    recur everywhere, so matching them would link unrelated steps.
    """
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) >= 10
    if isinstance(value, float):
        return abs(value) >= 10 or not value.is_integer()
    if isinstance(value, str):
        return len(value.strip()) >= 4
    return False


def _leaf(pointer: str) -> str:
    return pointer.rsplit("/", 1)[-1]


class Redactor:
    _patterns = (
        re.compile(r"\b(?:gsk_|sk-)[A-Za-z0-9_-]{12,}\b"),
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
        re.compile(
            # Don't redact digit sequences embedded in identifiers such as
            # PROMPT-8496625705CA. A phone number in prose is still preceded by
            # whitespace or punctuation and remains covered by this pattern.
            r"(?<![\w₹$-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?"
            r"\d{3}[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
        ),
    )

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        self._secrets = tuple(secret for secret in secrets if secret)

    def __call__(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, tuple):
            return [self(item) for item in value]
        if not isinstance(value, str):
            return value
        redacted = value
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        for pattern in self._patterns:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted


@dataclass(slots=True)
class CallCapture:
    request_key: str
    input: Any
    output: Any
    cache_status: CacheStatus
    reasoning: Any = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_cached: int | None = None
    finish_reason: str | None = None
    retries: int = 0


@dataclass(slots=True)
class EdgeCapture:
    src_addr: str
    src_pointer: str | None
    dst_addr: str
    dst_pointer: str | None
    value_hash: str | None
    key: str | None
    version: int | None
    kind: str


@dataclass(slots=True)
class StepScope:
    run: RunSession
    addr: str
    kind: str
    name: str
    agent_role: str | None = None
    seq: int = 0
    state_before: str = ""
    started: float = 0.0
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    calls: list[CallCapture] = field(default_factory=list)
    edges: list[EdgeCapture] = field(default_factory=list)
    error_type: str | None = None
    _token: contextvars.Token[StepScope | None] | None = None

    def __enter__(self) -> StepScope:
        if self.addr in self.run.seen_addresses:
            raise ValueError(f"duplicate step address in run: {self.addr}")
        self.run.seen_addresses.add(self.addr)
        self.seq = self.run.next_sequence()
        self.state_before = self.run.state.checkpoint()
        if self.run.replay_policy is not None:
            self.run.replay_policy.before_step(self.addr, self.seq, self.state_before)
        self.started = time.perf_counter()
        self._token = _current_step.set(self)
        return self

    def __exit__(self, error_type: type[BaseException] | None, *_: object) -> None:
        if error_type is not None:
            self.error_type = error_type.__name__
        if self._token is not None:
            _current_step.reset(self._token)
        self._persist()

    async def __aenter__(self) -> StepScope:
        return self.__enter__()

    async def __aexit__(self, error_type: type[BaseException] | None, *_: object) -> None:
        self.__exit__(error_type)

    def capture(self, call: CallCapture) -> None:
        self.calls.append(call)
        for pointer, value in iter_scalar_pointers(call.input):
            value = self.run.redactor(value)
            if not is_informative(value):
                continue
            value_ref = content_hash(value)
            candidates = [
                producer
                for producer in self.run.value_producers.get(value_ref, [])
                if producer[0] != self.addr
            ]
            # When several steps emitted the same value, prefer producers whose field
            # name matches the consuming field; keep all of them only if none does.
            same_field = [p for p in candidates if _leaf(p[1]) == _leaf(pointer)]
            for producer_addr, producer_pointer in same_field or candidates:
                self.edges.append(
                    EdgeCapture(
                        src_addr=producer_addr,
                        src_pointer=producer_pointer,
                        dst_addr=self.addr,
                        dst_pointer=f"/input{pointer if pointer != '/' else ''}",
                        value_hash=value_ref,
                        key=None,
                        version=None,
                        kind="inferred",
                    )
                )

    def _persist(self) -> None:
        elapsed_ms = (time.perf_counter() - self.started) * 1000
        state_after = self.run.state.checkpoint()
        inputs = [capture.input for capture in self.calls]
        outputs = [capture.output for capture in self.calls]
        reasons = [capture.reasoning for capture in self.calls if capture.reasoning is not None]
        input_payload = inputs[0] if len(inputs) == 1 else inputs
        output_payload = outputs[0] if len(outputs) == 1 else outputs
        redacted_input = self.run.redactor(input_payload)
        redacted_output = self.run.redactor(output_payload)
        input_hash = self.run.store.save_json(redacted_input) if self.calls else None
        output_hash = self.run.store.save_json(redacted_output) if self.calls else None
        reasoning_hash = self.run.store.save_json(self.run.redactor(reasons)) if reasons else None
        request_key = (
            self.calls[0].request_key
            if len(self.calls) == 1
            else content_hash([capture.request_key for capture in self.calls])
            if self.calls
            else None
        )
        statuses = {capture.cache_status for capture in self.calls}
        cache_status: CacheStatus = "live"
        for candidate in ("edited", "live", "invalidated", "cached"):
            if candidate in statuses:
                cache_status = candidate  # type: ignore[assignment]
                break
        tokens_in = sum(capture.tokens_in or 0 for capture in self.calls) or None
        tokens_out = sum(capture.tokens_out or 0 for capture in self.calls) or None
        tokens_cached = sum(capture.tokens_cached or 0 for capture in self.calls) or None
        finish_reason = next(
            (capture.finish_reason for capture in reversed(self.calls) if capture.finish_reason),
            None,
        )
        retries = sum(capture.retries for capture in self.calls)
        self.run.database.execute(
            """
            INSERT INTO steps(
                run_id, addr, seq, kind, name, agent_role, request_key, input_hash,
                output_hash, reasoning_hash, state_before, state_after, reads_json,
                writes_json, cache_status, tokens_in, tokens_out, tokens_cached,
                latency_ms, finish_reason, error_type, retries
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.run.run_id,
                self.addr,
                self.seq,
                self.kind,
                self.name,
                self.agent_role,
                request_key,
                input_hash,
                output_hash,
                reasoning_hash,
                self.state_before,
                state_after,
                json.dumps(sorted(self.reads)),
                json.dumps(sorted(self.writes)),
                cache_status,
                tokens_in,
                tokens_out,
                tokens_cached,
                elapsed_ms,
                finish_reason,
                self.error_type,
                retries,
            ),
        )
        if self.edges:
            self.run.database.executemany(
                """
                INSERT OR IGNORE INTO edges(
                    run_id, src_addr, src_pointer, dst_addr, dst_pointer,
                    value_hash, key, version, kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        self.run.run_id,
                        edge.src_addr,
                        edge.src_pointer,
                        edge.dst_addr,
                        edge.dst_pointer,
                        edge.value_hash,
                        edge.key,
                        edge.version,
                        edge.kind,
                    )
                    for edge in self.edges
                ],
            )
        if self.calls:
            for pointer, value in iter_scalar_pointers(redacted_output):
                if not is_informative(value):
                    continue
                value_ref = content_hash(value)
                self.run.value_producers.setdefault(value_ref, []).append(
                    (self.addr, f"/output{pointer if pointer != '/' else ''}")
                )
        if self.run.replay_policy is not None:
            self.run.replay_policy.after_step(
                self.addr,
                cache_status,
                state_after,
                ms=elapsed_ms,
                tokens=(tokens_in or 0) + (tokens_out or 0),
            )
        if self.run.span_exporter is not None:
            self.run.span_exporter(
                {
                    "name": self.name,
                    "openinference.span.kind": self.kind.upper(),
                    "blackbox.run_id": self.run.run_id,
                    "blackbox.addr": self.addr,
                    "blackbox.cache_status": cache_status,
                    "latency_ms": elapsed_ms,
                }
            )


def _normalize_tool_call_ids(value: Any, addr: str) -> Any:
    mapping: dict[str, str] = {}

    def visit(item: Any) -> Any:
        if isinstance(item, list):
            return [visit(child) for child in item]
        if not isinstance(item, dict):
            return item
        normalized: dict[str, Any] = {}
        for key, child in item.items():
            if key == "tool_call_id" and isinstance(child, str):
                replacement = mapping.setdefault(child, f"tc_{addr}_{len(mapping)}")
                normalized[key] = replacement
            elif key == "tool_calls" and isinstance(child, list):
                calls = []
                for call in child:
                    if isinstance(call, dict) and isinstance(call.get("id"), str):
                        call = dict(call)
                        call["id"] = mapping.setdefault(call["id"], f"tc_{addr}_{len(mapping)}")
                    calls.append(visit(call))
                normalized[key] = calls
            else:
                normalized[key] = visit(child)
        return normalized

    return visit(value)


def chat_request_key(request: dict[str, Any], addr: str) -> str:
    return content_hash({"kind": "llm", "request": _normalize_tool_call_ids(request, addr)})


def _strip_reasoning(response: Any) -> Any:
    if not isinstance(response, dict):
        return response
    cleaned = {key: value for key, value in response.items() if key != "reasoning"}
    choices = []
    for choice in response.get("choices", []):
        if isinstance(choice, dict) and isinstance(choice.get("message"), dict):
            message = {k: v for k, v in choice["message"].items() if k != "reasoning"}
            choice = {**choice, "message": message}
        choices.append(choice)
    if "choices" in response:
        cleaned["choices"] = choices
    return cleaned


class RunSession:
    def __init__(
        self,
        recorder: Recorder,
        *,
        agent: str,
        task_id: str,
        seed: int,
        run_id: str | None,
        initial_state: dict[str, Any] | None,
        model: str | None,
        parent_run_id: str | None,
        fork_id: str | None,
        replay_policy: Any | None,
    ) -> None:
        self.recorder = recorder
        self.database = recorder.database
        self.store = recorder.store
        self.redactor = recorder.redactor
        self.span_exporter = recorder.span_exporter
        self.run_id = run_id or uuid_module.uuid4().hex
        self.agent = agent
        self.task_id = task_id
        self.seed = seed
        self.model = model
        self.parent_run_id = parent_run_id
        self.fork_id = fork_id
        self.replay_policy = replay_policy
        self.state = State(self, initial_state)
        self.seen_addresses: set[str] = set()
        self.value_producers: dict[str, list[tuple[str, str]]] = {}
        self.outcome: str | None = None
        self.score: float | None = None
        self.checker_reason: str | None = None
        self._sequence = 0
        self._token: contextvars.Token[RunSession | None] | None = None
        self._nondeterminism_counter = 0
        self._random = random_module.Random(seed)

    def __enter__(self) -> RunSession:
        mode = "replay" if self.replay_policy is not None else self.recorder.mode
        self.database.execute(
            """
            INSERT INTO runs(
                run_id, parent_run_id, fork_id, agent, agent_version, task_id,
                model, seed, mode, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.run_id,
                self.parent_run_id,
                self.fork_id,
                self.agent,
                "dev",
                self.task_id,
                self.model,
                self.seed,
                mode,
                _utc_now(),
            ),
        )
        self._token = _current_run.set(self)
        return self

    def __exit__(self, error_type: type[BaseException] | None, *_: object) -> None:
        if self._token is not None:
            _current_run.reset(self._token)
        if error_type is not None and self.outcome is None:
            self.outcome = "failed"
            self.checker_reason = error_type.__name__
        self.database.execute(
            """
            UPDATE runs
            SET outcome = ?, score = ?, checker_reason = ?, ended_at = ?
            WHERE run_id = ?
            """,
            (
                self.outcome,
                self.score,
                self.checker_reason,
                _utc_now(),
                self.run_id,
            ),
        )

    def next_sequence(self) -> int:
        sequence = self._sequence
        self._sequence += 1
        return sequence

    def step(
        self,
        addr: str,
        kind: str,
        name: str | None = None,
        *,
        agent_role: str | None = None,
    ) -> StepScope:
        return StepScope(self, addr, kind, name or addr.split("/")[-1], agent_role)

    def set_outcome(
        self,
        passed: bool | str,
        *,
        score: float | None = None,
        reason: str | None = None,
    ) -> None:
        self.outcome = ("passed" if passed else "failed") if isinstance(passed, bool) else passed
        self.score = score
        self.checker_reason = reason

    def snapshot(self, state: dict[str, Any]) -> str:
        return self.store.save_checkpoint(self.redactor(state))

    def state_read(self, key: str, value: Any, version: int, producer: str | None) -> None:
        scope = _current_step.get()
        if scope is None or scope.run is not self:
            return
        scope.reads.add(f"{key}@v{version}")
        if producer and producer != scope.addr:
            scope.edges.append(
                EdgeCapture(
                    src_addr=producer,
                    src_pointer=f"/state/{_escape_pointer(key)}",
                    dst_addr=scope.addr,
                    dst_pointer=f"/state/{_escape_pointer(key)}",
                    value_hash=content_hash(self.redactor(value)),
                    key=key,
                    version=version,
                    kind="state",
                )
            )

    def state_write(self, key: str, value: Any, version: int) -> str | None:
        scope = _current_step.get()
        if scope is None or scope.run is not self:
            return None
        scope.writes.add(f"{key}@v{version}")
        return scope.addr

    def _require_step(self) -> StepScope:
        scope = _current_step.get()
        if scope is None or scope.run is not self:
            raise RuntimeError("bb.chat and bb.tool must run inside a bb.step scope")
        return scope

    async def _resolve(
        self,
        *,
        addr: str,
        kind: str,
        request_key: str,
        live: Callable[[], Awaitable[Any]],
    ) -> tuple[Any, CacheStatus]:
        if self.replay_policy is not None:
            return await self.replay_policy.resolve(addr, kind, request_key, live)
        return await live(), "live"

    async def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        response_format: Mapping[str, Any] | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        scope = self._require_step()
        request: dict[str, Any] = {
            "messages": list(messages),
            "model": model or self.recorder.settings.agent_model,
            **params,
        }
        if tools is not None:
            request["tools"] = list(tools)
        if response_format is not None:
            request["response_format"] = dict(response_format)
        if self.replay_policy is not None:
            request = self.replay_policy.transform_chat(scope.addr, request)
        request_key = chat_request_key(request, scope.addr)
        hinted = False

        async def live() -> dict[str, Any]:
            nonlocal hinted
            if self.recorder.mode == "recorded":
                raise RuntimeError("recorded mode refuses live LLM calls")
            if self.recorder.llm_client is None:
                raise RuntimeError("no LLM client configured")
            call = request
            if self.replay_policy is not None:
                call = self.replay_policy.live_chat_request(scope.addr, request)
                hinted = call is not request
            return await self.recorder.llm_client.chat(**call)

        response, status = await self._resolve(
            addr=scope.addr,
            kind="llm",
            request_key=request_key,
            live=live,
        )
        if hinted:
            # The model's reasoning may restate the hidden instruction; never store it.
            response = _strip_reasoning(response)
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        choice = response.get("choices", [{}])[0] if isinstance(response, dict) else {}
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        reasoning = message.get("reasoning") or response.get("reasoning")
        scope.capture(
            CallCapture(
                request_key=request_key,
                input=request,
                output=response,
                cache_status=status,
                reasoning=reasoning,
                tokens_in=usage.get("prompt_tokens"),
                tokens_out=usage.get("completion_tokens"),
                tokens_cached=usage.get("cached_tokens"),
                finish_reason=choice.get("finish_reason") if isinstance(choice, dict) else None,
            )
        )
        response_hash = self.store.save_json(self.redactor(response))
        # An edited response is not the answer to its request; caching it would
        # poison exact-hash hits for every later replay of the same request.
        if status != "edited":
            self.database.execute(
                """
                INSERT OR IGNORE INTO cassette(request_key, kind, response_hash, model, created_at)
                VALUES (?, 'llm', ?, ?, ?)
                """,
                (request_key, response_hash, request["model"], _utc_now()),
            )
        return response

    async def tool(self, function: Callable[..., Any], /, **args: Any) -> Any:
        scope = self._require_step()
        name = getattr(function, "__name__", function.__class__.__name__)
        version = getattr(function, "__blackbox_version__", "1")
        request = {"name": name, "tool_version": str(version), "args": args}
        if self.replay_policy is not None:
            request = self.replay_policy.transform_tool(scope.addr, request)
        request_key = content_hash({"kind": "tool", "request": request})

        async def live() -> Any:
            result = function(**request["args"])
            return await result if inspect.isawaitable(result) else result

        output, status = await self._resolve(
            addr=scope.addr,
            kind="tool",
            request_key=request_key,
            live=live,
        )
        scope.capture(CallCapture(request_key, request, output, status))
        response_hash = self.store.save_json(self.redactor(output))
        if status != "edited":
            self.database.execute(
                """
                INSERT OR IGNORE INTO cassette(request_key, kind, response_hash, model, created_at)
                VALUES (?, 'tool', ?, NULL, ?)
                """,
                (request_key, response_hash, _utc_now()),
            )
        return output

    async def _recorded_value(self, name: str, factory: Callable[[], Any]) -> Any:
        scope = self._require_step()
        ordinal = self._nondeterminism_counter
        self._nondeterminism_counter += 1
        request = {"name": name, "ordinal": ordinal}
        request_key = content_hash({"kind": "value", "request": request})

        async def live() -> Any:
            return factory()

        output, status = await self._resolve(
            addr=scope.addr,
            kind="value",
            request_key=request_key,
            live=live,
        )
        scope.capture(CallCapture(request_key, request, output, status))
        response_hash = self.store.save_json(output)
        self.database.execute(
            """
            INSERT OR IGNORE INTO cassette(request_key, kind, response_hash, model, created_at)
            VALUES (?, 'value', ?, NULL, ?)
            """,
            (request_key, response_hash, _utc_now()),
        )
        return output

    async def now(self) -> str:
        return await self._recorded_value("now", _utc_now)

    async def uuid(self) -> str:
        return await self._recorded_value("uuid", lambda: str(uuid_module.uuid4()))

    async def random(self) -> float:
        return await self._recorded_value("random", self._random.random)


class Recorder:
    def __init__(
        self,
        data_dir: str | Path,
        *,
        mode: str | None = None,
        settings: Settings | None = None,
        llm_client: Any | None = None,
        secrets: Sequence[str] = (),
        span_exporter: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings = settings or Settings.load()
        self.mode = mode or self.settings.mode
        if self.mode not in {"live", "recorded", "offline"}:
            raise ValueError("mode must be live, recorded, or offline")
        self.store = Store(self.data_dir / "content")
        self.database = SQLiteDatabase(self.data_dir / "blackbox.db")
        default_secrets = (self.settings.groq_api_key,) if self.settings.groq_api_key else ()
        self.redactor = Redactor((*default_secrets, *secrets))
        self.llm_client = llm_client
        self.span_exporter = span_exporter

    def run(
        self,
        agent: str,
        task_id: str,
        seed: int,
        *,
        run_id: str | None = None,
        initial_state: dict[str, Any] | None = None,
        model: str | None = None,
        parent_run_id: str | None = None,
        fork_id: str | None = None,
        replay_policy: Any | None = None,
    ) -> RunSession:
        return RunSession(
            self,
            agent=agent,
            task_id=task_id,
            seed=seed,
            run_id=run_id,
            initial_state=initial_state,
            model=model,
            parent_run_id=parent_run_id,
            fork_id=fork_id,
            replay_policy=replay_policy,
        )

    async def aclose(self) -> None:
        if isinstance(self.llm_client, AsyncLLMClient):
            await self.llm_client.aclose()
        self.database.close()

    def close(self) -> None:
        self.database.close()


def current_run() -> RunSession:
    session = _current_run.get()
    if session is None:
        raise RuntimeError("no active Black Box run")
    return session
