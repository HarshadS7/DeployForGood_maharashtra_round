"""Black Box API contract: the single source of truth for the backend, MCP server, verifier,
evaluation pipeline and the web UI.

Conventions
-----------
* Every model forbids unknown fields. A field that is missing from a payload is an error.
* Response models declare no defaults: every field is always serialized, and "not available"
  is an explicit `null` or an empty list. Only request models (and the small nested
  `ForkEdit`) carry defaults, so a TypeScript client only has to supply what differs.
* Rates and probabilities are fractions in [0, 1]. Durations are milliseconds. Timestamps are
  timezone-aware ISO-8601.
* JSON pointers (RFC 6901) are relative to a *step document*, see `Citation`.
* `fixture: true` on a payload means hand-authored illustrative content, never a measurement.

`docs/api.md` describes the API; `python -m server.contract_export` turns
these models into `server/contract.schema.json` and `web/lib/contract.ts`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union, get_args

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from blackbox.replay import wilson_interval

CONTRACT_VERSION = "1.0.0"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

Mode = Literal["live", "recorded", "offline"]
RunMode = Literal["live", "recorded", "offline", "replay"]
RunStatus = Literal["passed", "failed", "running"]
RunOrigin = Literal["natural", "injected", "fork", "imported"]
ClientKind = Literal["llm", "deterministic_standin", "imported"]
Split = Literal["S0", "S1", "S3", "S4", "S5"]
LabelSource = Literal["injected", "natural_auto", "human", "verified"]
FaultFamily = Literal["tool", "retrieval", "decision", "coordination"]
StepKind = Literal["llm", "tool", "retrieval", "state", "value"]
CacheStatus = Literal["cached", "invalidated", "live", "edited"]
StepPhase = Literal["queued", "running", "done", "diverged"]
EdgeKind = Literal["state", "message", "inferred"]
EditKind = Literal[
    "override_output", "patch_tool_args", "patch_prompt", "swap_model", "patch_tool_result"
]
ReplayMode = Literal["cone", "prefix", "full"]
Verdict = Literal["VERIFIED", "REFUTED", "INCONCLUSIVE"]
Branch = Literal["fix", "control"]
DiffStatus = Literal["same", "cached", "changed", "new", "removed"]
DamageTag = Literal["root", "symptom", "unaffected"]
SchemaRule = Literal[
    "empty_query",
    "empty_result",
    "swallowed_error",
    "repeated_call",
    "schema_invalid",
    "non_finite_or_out_of_range",
    "unit_token_mismatch",
    "stale_as_of",
    "error_status_in_output",
    "other",
]
Severity = Literal["error", "warning", "info"]
SuspectFlag = Literal[
    "visible_failure",
    "error_recovered",
    "schema_violation",
    "first_anomaly",
    "on_damage_path",
]
AbstainReason = Literal["margin_too_small", "set_too_large", "no_signal", "model_unavailable"]
Stage = Literal["localize", "attribute", "propose", "verify"]
StageState = Literal["pending", "active", "done", "abstained", "blocked"]
EditSource = Literal["schema_rule", "oracle", "nearest_twin", "precedent", "llm", "manual"]
JobStatus = Literal["queued", "running", "complete", "failed"]
ForkStatus = Literal["queued", "running", "complete", "error"]
TimelineKind = Literal["original", "control", "fork"]
StateKeyStatus = Literal["unchanged", "changed", "added", "removed"]
AlignMode = Literal["address", "lcs"]
ErrorCode = Literal[
    "bad_request",
    "validation_error",
    "not_found",
    "conflict",
    "live_call_refused",
    "replay_divergence",
    "not_verified",
    "not_applicable",
    "rate_limited",
    "unavailable",
    "unsupported",
    "internal_error",
]
EvalStatus = Literal["ran", "not_run"]
MethodKind = Literal["ranker", "baseline", "llm_judge", "literature"]
ModelBackend = Literal["lightgbm", "sklearn-hgb"]
ClaimSource = Literal["template", "llm"]

# Named enumerations exported to TypeScript as `<Name>Values` plus a derived union type.
ENUMS: dict[str, tuple[str, ...]] = {
    name: get_args(value)
    for name, value in sorted(globals().items())
    if getattr(value, "__origin__", None) is Literal
}

# Stable step address such as 'fx/tool#1'. URL-encode it inside request paths.
Addr = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=r"^\S+$")]
RunId = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
# RFC 6901 JSON Pointer.
JsonPointer = Annotated[str, StringConstraints(max_length=1024, pattern=r"^(/([^/~]|~[01])*)*$")]
Fraction = Annotated[float, Field(ge=0, le=1)]
Millis = Annotated[float, Field(ge=0)]
Count = Annotated[int, Field(ge=0)]
Interval = tuple[Fraction, Fraction]


# ---------------------------------------------------------------------------
# Errors and pagination
# ---------------------------------------------------------------------------


class ValidationIssue(ContractModel):
    """One request-validation problem (the shape of a FastAPI 422 entry, normalised)."""

    loc: list[str | int]
    message: str
    type: str


class ErrorBody(ContractModel):
    """Machine-readable error. `code` is stable; `message` is for people."""

    code: ErrorCode
    message: str
    status: int = Field(ge=400, le=599)
    hint: str | None = Field(description="What the caller can do next, when there is a next step.")
    issues: list[ValidationIssue]
    context: dict[str, Any] | None
    request_id: str | None


class ErrorResponse(ContractModel):
    """Uniform body of every non-2xx response, including the SSE `error` event's data."""

    error: ErrorBody


class PageMeta(ContractModel):
    """Pagination convention: `limit`/`offset` query parameters, total always reported."""

    total: Count
    limit: int = Field(ge=1, le=200)
    offset: Count
    next_offset: int | None = Field(
        ge=0, description="Offset of the next page, or null on the last page."
    )

    @model_validator(mode="after")
    def _consistent(self) -> PageMeta:
        has_more = self.offset + self.limit < self.total
        if has_more != (self.next_offset is not None):
            raise ValueError("next_offset must be set exactly when more items remain")
        if self.next_offset is not None and self.next_offset != self.offset + self.limit:
            raise ValueError("next_offset must equal offset + limit")
        return self


# ---------------------------------------------------------------------------
# Health and agents
# ---------------------------------------------------------------------------


class Capabilities(ContractModel):
    """What this deployment can do. The UI disables controls instead of letting them fail."""

    diagnose: bool
    fork: bool = Field(description="POST /forks accepts edits (false for the static bundle).")
    live_calls: bool = Field(description="Replay may call a real model or tool.")
    verify: bool
    export_test: bool
    label: bool
    ingest_otlp: bool
    eval: bool


class HealthCounts(ContractModel):
    runs: Count
    steps: Count
    forks: Count
    labels: Count


class AppHealth(ContractModel):
    """GET /health. Cheap enough for a platform health check."""

    status: Literal["ok", "degraded"]
    api_version: str
    mode: Mode
    static_bundle: bool = Field(description="True when served from web/public/recorded.")
    model_version: str | None = Field(description="Diagnoser artifact version, null if absent.")
    dataset_version: str | None
    capabilities: Capabilities
    counts: HealthCounts
    started_at: AwareDatetime
    notes: list[str]


class AgentInfo(ContractModel):
    agent_id: str
    display_name: str
    description: str
    client: ClientKind
    models: list[str]
    runs: Count
    passed: Count
    failed: Count
    typical_steps: int | None


class AgentList(ContractModel):
    items: list[AgentInfo]


class TaskRunRequest(ContractModel):
    """An offline TripCrew task submitted as natural language."""

    prompt: str = Field(min_length=24, max_length=1200)
    inject_stale_fx: bool = False


class TaskRunResponse(ContractModel):
    run_id: RunId
    status: RunStatus
    task: str
    steps: Count


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class Cost(ContractModel):
    """Token and money cost of one run. `usd` is null when the model has no known price."""

    llm_calls: Count
    tokens_in: Count
    tokens_out: Count
    tokens_cached: Count
    usd: float | None = Field(ge=0)


class SuspectBrief(ContractModel):
    addr: Addr
    name: str
    probability: Fraction


class LabelInfo(ContractModel):
    """Ground truth for a run, when known. Hidden (null) in blind/Label mode."""

    source: LabelSource
    root_addr: Addr
    fault_code: str | None = Field(description="Fault Forge operator code such as 'T2'.")
    fault_type: str | None
    fault_family: FaultFamily | None
    held_out: bool | None = Field(description="True for fault types never seen in training.")
    recovered: bool = Field(description="The agent recovered, so the run still passed.")
    manifest_addr: Addr | None = Field(description="First step whose behaviour changed.")
    verified: bool
    confidence: Literal["high", "low"] | None


class RunSummary(ContractModel):
    """One row of the Runs table."""

    run_id: RunId
    status: RunStatus
    agent: str
    task_id: str
    task: str = Field(description="One-line task summary for the table.")
    origin: RunOrigin
    parent_run_id: RunId | None
    fork_id: str | None
    mode: RunMode
    model: str | None
    client: ClientKind = Field(
        description="'deterministic_standin' runs come from an offline test double, not a model."
    )
    seed: int | None
    steps: Count
    duration_ms: Millis
    cost: Cost
    started_at: AwareDatetime
    ended_at: AwareDatetime | None
    score: float | None
    checker_reason: str | None
    split: Split | None
    risk: Fraction | None = Field(description="Run-level failure probability from the detector.")
    top_suspect: SuspectBrief | None
    failure_signature: str | None = Field(
        description="Groups runs that fail alike: '<agent>:<suspect step name>:<reason class>'."
    )
    label: LabelInfo | None


class RunFacets(ContractModel):
    """Counts behind the filter chips, computed over the filtered set before paging."""

    agents: dict[str, Count]
    outcomes: dict[str, Count]
    origins: dict[str, Count]
    splits: dict[str, Count]


class RunList(PageMeta):
    """GET /runs. Fork sample runs (`origin: fork`) are left out unless filtered for."""

    items: list[RunSummary]
    facets: RunFacets
    fixture: bool = Field(
        description="True when risk, top_suspect and failure_signature are authored illustrations."
    )


class RunListQuery(ContractModel):
    """Query parameters of GET /runs."""

    agent: str | None = None
    outcome: RunStatus | None = None
    origin: RunOrigin | None = None
    split: Split | None = None
    label_source: LabelSource | None = None
    failure_signature: str | None = None
    q: str | None = Field(default=None, max_length=200, description="Free text over task/run id.")
    sort: Literal["started_at", "duration_ms", "risk", "steps"] = "started_at"
    order: Literal["asc", "desc"] = "desc"
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class FailureGroup(ContractModel):
    """'14 runs fail like this': failed runs sharing one failure_signature."""

    signature: str
    label: str
    agent: str
    count: int = Field(ge=1)
    top_suspect_name: str | None
    fault_type: str | None
    example_run_ids: list[RunId]


class FailureGroupList(ContractModel):
    total_failed: Count
    items: list[FailureGroup]
    fixture: bool


class Citation(ContractModel):
    """A pointer to evidence. `json_pointer` is relative to the step document.

    The step document is `{input, output, reasoning, state_before, state_after}` of
    `StepDetail`. Edges recorded by the SDK use the shorthand `/state/<key>`, which means
    `state_after` on the producing side and `state_before` on the consuming side.
    """

    addr: Addr
    json_pointer: JsonPointer | None


class RuleViolation(ContractModel):
    """A deterministic schema or protocol rule that failed. Evidence, not model prose."""

    rule: SchemaRule
    severity: Severity
    message: str
    citation: Citation
    evidence: dict[str, Any] | None


class TokenUsage(ContractModel):
    input: Count
    output: Count
    cached: Count


class StepError(ContractModel):
    type: str
    message: str | None
    recovered: bool | None = Field(
        description="True when later steps ran normally; null when unknown."
    )


class StepHashes(ContractModel):
    """Content hashes (hex SHA-256) of the stored payloads, for exact-match reasoning."""

    request_key: str | None
    input: str | None
    output: str | None
    reasoning: str | None
    state_before: str
    state_after: str | None


class StepDetail(ContractModel):
    """Everything recorded about one step. The same shape serves list and detail views."""

    addr: Addr
    seq: Count
    kind: StepKind
    name: str
    agent_role: str | None
    input: Any | None
    output: Any | None
    reasoning: list[str] = Field(description="Reasoning text per call; kept as evidence only.")
    state_before: dict[str, Any]
    state_after: dict[str, Any]
    reads: list[str] = Field(description="State keys read, as 'key@vN'.")
    writes: list[str] = Field(description="State keys written, as 'key@vN'.")
    has_call: bool = Field(description="False for pure state steps with no LLM/tool call.")
    cache_status: CacheStatus
    tokens: TokenUsage | None
    latency_ms: Millis
    finish_reason: str | None
    retries: Count
    error: StepError | None
    rule_violations: list[RuleViolation]
    hashes: StepHashes


class Edge(ContractModel):
    """A dependency between two steps. Pointers are relative to each step's document."""

    src_addr: Addr
    src_pointer: JsonPointer | None
    dst_addr: Addr
    dst_pointer: JsonPointer | None
    kind: EdgeKind
    key: str | None
    version: int | None
    value_hash: str | None


class RunDetail(ContractModel):
    """GET /runs/{id}: the run, every step with payloads, and every provenance edge."""

    run: RunSummary
    task_text: str | None = Field(description="Full task statement given to the agent.")
    steps: list[StepDetail]
    edges: list[Edge]
    final_answer: Any | None
    fixture: bool = Field(description="True when the model-derived run fields are authored.")


# ---------------------------------------------------------------------------
# Value provenance (click a value, jump to its producer)
# ---------------------------------------------------------------------------


class ValueRef(ContractModel):
    addr: Addr
    json_pointer: JsonPointer
    value: Any | None
    value_hash: str | None


class ProducerCandidate(ContractModel):
    addr: Addr
    name: str
    json_pointer: JsonPointer | None
    edge_kind: EdgeKind
    key: str | None
    version: int | None
    value_hash: str | None


class ConsumerHop(Edge):
    """An edge on the forward path from the clicked value; `depth` 1 is a direct consumer."""

    depth: int = Field(ge=1)


class ValueProvenance(ContractModel):
    """GET /runs/{id}/provenance. Never invents a single origin for an ambiguous value."""

    run_id: RunId
    target: ValueRef
    producer: ProducerCandidate | None = Field(
        description="The exact origin; set only when there is exactly one candidate."
    )
    candidates: list[ProducerCandidate]
    ambiguous: bool
    consumers: list[ConsumerHop]
    reaches_final_answer: bool

    @model_validator(mode="after")
    def _origin_rules(self) -> ValueProvenance:
        if self.ambiguous != (len(self.candidates) > 1):
            raise ValueError("ambiguous must be true exactly when there are several candidates")
        if self.producer is not None and len(self.candidates) != 1:
            raise ValueError("producer is only allowed when there is exactly one candidate")
        if len(self.candidates) == 1 and self.producer is None:
            raise ValueError("a unique candidate must be reported as the producer")
        return self


# ---------------------------------------------------------------------------
# Verification arithmetic shared by diagnosis, forks and streams
# ---------------------------------------------------------------------------


class PassRate(ContractModel):
    """k of n passes with the Wilson 95% interval. The interval is checked, not trusted."""

    passed: Count
    total: int = Field(ge=1)
    rate: Fraction
    ci_low: Fraction
    ci_high: Fraction

    @model_validator(mode="after")
    def _arithmetic(self) -> PassRate:
        if self.passed > self.total:
            raise ValueError("passed cannot exceed total")
        low, high = wilson_interval(self.passed, self.total)
        if abs(self.rate - self.passed / self.total) > 1e-9:
            raise ValueError("rate must equal passed / total")
        if abs(self.ci_low - low) > 1e-6 or abs(self.ci_high - high) > 1e-6:
            raise ValueError("ci_low/ci_high must be the Wilson 95% interval of passed/total")
        return self


def pass_rate(passed: int, total: int) -> PassRate:
    """Build a PassRate with its Wilson interval."""
    low, high = wilson_interval(passed, total)
    return PassRate(passed=passed, total=total, rate=passed / total, ci_low=low, ci_high=high)


def decide_verdict(
    k: int, fix: PassRate, control: PassRate | None, *, known_good: bool
) -> Verdict | None:
    """The replay engine's verdict rule, restated so streams and fixtures can be checked.

    `None` without a paired control: the result is a preview and can never be VERIFIED.
    """
    if control is None:
        return None
    if k >= 3 and fix.ci_low > control.ci_high:
        return "VERIFIED"
    # Equal zero-pass branches do not show that the edit is irrelevant: both may
    # contain another failure, so there is no counterfactual contrast to interpret.
    if k >= 3 and known_good and fix.passed == 0 and control.passed > fix.passed:
        return "REFUTED"
    return "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# Fork requests and replay prediction
# ---------------------------------------------------------------------------


class ForkEdit(ContractModel):
    """One change applied to one step of the base run.

    `value` by kind: `override_output` and `patch_tool_result` take the replacement
    payload; `patch_tool_args` an object merged into the tool arguments; `patch_prompt` a
    string appended to the system prompt or an object merged into the chat request;
    `swap_model` the model id.
    """

    addr: Addr
    kind: EditKind
    value: Any
    known_good: bool = Field(
        default=False,
        description="The value is an oracle or nearest-passing-twin value, which enables REFUTED.",
    )

    @model_validator(mode="after")
    def _value_shape(self) -> ForkEdit:
        if self.kind == "patch_tool_args" and not isinstance(self.value, dict):
            raise ValueError("patch_tool_args needs an object value")
        if self.kind == "patch_prompt" and not isinstance(self.value, (str, dict)):
            raise ValueError("patch_prompt needs a string or object value")
        if self.kind == "swap_model" and not (isinstance(self.value, str) and self.value.strip()):
            raise ValueError("swap_model needs a non-empty model id")
        return self


class ForkRequest(ContractModel):
    """POST /forks and POST /replay/predict.

    `edits` non-empty: *Fork with edit*. `edits` empty: *Re-run unchanged*, which resamples
    the cone of `resample_from` with no edit and is the paired control on its own.
    """

    base_run_id: RunId
    edits: list[ForkEdit] = Field(default_factory=list, max_length=8)
    resample_from: Addr | None = Field(
        default=None, description="Required when edits is empty; forbidden otherwise."
    )
    mode: ReplayMode = "cone"
    samples: int = Field(default=5, ge=1, le=10)
    control: bool = Field(
        default=True,
        description="Run K unchanged samples with identical seeds. Without it the result is "
        "a preview and is never VERIFIED.",
    )
    hypothesis: str | None = Field(default=None, max_length=500)
    branch_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

    @model_validator(mode="after")
    def _shape(self) -> ForkRequest:
        addrs = [edit.addr for edit in self.edits]
        if len(addrs) != len(set(addrs)):
            raise ValueError("only one edit per step is supported")
        if not self.edits and self.resample_from is None:
            raise ValueError("resample_from is required when there are no edits")
        if self.edits and self.resample_from is not None:
            raise ValueError("resample_from is only for re-running unchanged")
        if not self.edits and self.control:
            raise ValueError("an unchanged re-run is itself the control; set control=false")
        return self


class StepPrediction(ContractModel):
    addr: Addr
    expected: CacheStatus
    has_call: bool


class ReplayPrediction(ContractModel):
    """POST /replay/predict: what a fork will cost, before running it.

    `expected` is the static dependency cone: edited steps are `edited`, their transitive
    consumers `invalidated`, everything else `cached`. Early cutoff can only shrink it.
    """

    base_run_id: RunId
    mode: ReplayMode
    samples: int = Field(ge=1, le=10)
    total_steps: Count
    reexecute_steps: Count
    total_calls: Count = Field(description="LLM/tool/value calls in the base run.")
    reexecute_calls: Count
    per_step: list[StepPrediction]
    estimated_ms: Millis | None
    estimated_tokens: Count | None
    upper_bound: bool
    summary: str = Field(description="'Will re-run 4 of 13 calls (6 of 16 steps), about 12 ms.'")
    blocked: ErrorBody | None = Field(
        description="Why this fork would be refused in the current mode, or null."
    )

    @model_validator(mode="after")
    def _counts(self) -> ReplayPrediction:
        if self.reexecute_steps > self.total_steps or self.reexecute_calls > self.total_calls:
            raise ValueError("cannot re-execute more than the total")
        if len(self.per_step) != self.total_steps:
            raise ValueError("per_step must cover every step")
        return self


class ForkCreated(ContractModel):
    """202 response of POST /forks."""

    fork_id: str
    base_run_id: RunId
    branch_name: str
    status: ForkStatus
    stream_url: str
    prediction: ReplayPrediction | None
    created_at: AwareDatetime


# ---------------------------------------------------------------------------
# SSE events (GET /forks/{id}/stream)
# ---------------------------------------------------------------------------


class StepEventData(ContractModel):
    addr: Addr
    phase: StepPhase
    cache_status: CacheStatus | None = Field(
        description="Null until the status is known (e.g. a queued step that is not invalidated)."
    )
    ms: Millis | None
    tokens: Count | None
    sample: Count
    branch: Branch


class OutcomeEventData(ContractModel):
    run_id: RunId | None
    sample: Count
    branch: Branch
    passed: bool
    reason: str | None
    is_control: bool

    @model_validator(mode="after")
    def _control_flag(self) -> OutcomeEventData:
        if self.is_control != (self.branch == "control"):
            raise ValueError("is_control must match branch")
        return self


class SummaryEventData(ContractModel):
    """Final tally. Step counts are per edited run; tokens and time are summed over all runs."""

    fork_id: str
    reexecuted: Count
    cached: Count
    invalidated: Count
    total_steps: Count
    calls_reexecuted: Count
    calls_cached: Count
    tokens_saved: Count
    ms_saved: Millis
    fix_rate: Fraction
    fix_ci: Interval
    control_rate: Fraction | None
    control_ci: Interval | None
    k: int = Field(ge=1, le=10)
    verdict: Verdict | None
    preview: bool = Field(description="True when there is no paired control.")

    @model_validator(mode="after")
    def _preview(self) -> SummaryEventData:
        has_control = self.control_rate is not None
        if has_control != (self.control_ci is not None):
            raise ValueError("control_rate and control_ci must both be set or both be null")
        if self.preview == has_control:
            raise ValueError("preview must be true exactly when there is no control")
        if not has_control and self.verdict is not None:
            raise ValueError("a preview cannot carry a verdict")
        if has_control and self.verdict is None:
            raise ValueError("a controlled fork must carry a verdict")
        return self


class StepEvent(ContractModel):
    id: int = Field(ge=1)
    event: Literal["step"]
    data: StepEventData


class OutcomeEvent(ContractModel):
    id: int = Field(ge=1)
    event: Literal["outcome"]
    data: OutcomeEventData


class SummaryEvent(ContractModel):
    id: int = Field(ge=1)
    event: Literal["summary"]
    data: SummaryEventData


class StreamErrorEvent(ContractModel):
    id: int = Field(ge=1)
    event: Literal["error"]
    data: ErrorBody


ForkEvent = Annotated[
    Union[StepEvent, OutcomeEvent, SummaryEvent, StreamErrorEvent], Field(discriminator="event")
]


class ForkStreamRecording(ContractModel):
    """A recorded SSE transcript, used by the UI as a mock stream. Not an API response."""

    fork_id: str
    description: str
    provenance: Literal["engine_recording", "engine_recording_noisy_standin"]
    events: list[ForkEvent]


class StreamProblem(ContractModel):
    """A violated ordering guarantee found by `check_event_stream`."""

    event_id: int | None
    message: str


StreamEvent = Union[StepEvent, OutcomeEvent, SummaryEvent, StreamErrorEvent]


def check_event_stream(events: list[StreamEvent]) -> list[StreamProblem]:
    """Verify the ordering and consistency guarantees of the replay event contract.

    1. ids run 1..n without gaps.
    2. Per (branch, sample) run, a step address goes queued? -> running? -> done|diverged, once.
    3. A run has exactly one outcome and no step event after it.
    4. The stream ends with exactly one terminal event (summary xor error).
    5. The summary agrees with the outcome events: k, pass rates, Wilson intervals, verdict.
    """
    problems: list[StreamProblem] = []

    def flag(event_id: int | None, message: str) -> None:
        problems.append(StreamProblem(event_id=event_id, message=message))

    if not events:
        return [StreamProblem(event_id=None, message="empty stream")]
    for index, event in enumerate(events, start=1):
        if event.id != index:
            flag(event.id, f"expected id {index}")
    terminals = [event for event in events if event.event in {"summary", "error"}]
    if len(terminals) != 1 or events[-1] not in terminals:
        flag(events[-1].id, "stream must end with exactly one summary or error event")

    phase_rank = {"queued": 0, "running": 1, "done": 2, "diverged": 2}
    last_phase: dict[tuple[str, int, str], int] = {}
    finished: set[tuple[str, int]] = set()
    outcomes: dict[tuple[str, int], OutcomeEventData] = {}
    for event in events:
        if isinstance(event, StepEvent):
            run = (event.data.branch, event.data.sample)
            if run in finished:
                flag(event.id, f"step event after the outcome of {run}")
            key = (*run, event.data.addr)
            rank = phase_rank[event.data.phase]
            if rank <= last_phase.get(key, -1):
                flag(event.id, f"phase {event.data.phase} out of order for {event.data.addr}")
            last_phase[key] = rank
        elif isinstance(event, OutcomeEvent):
            run = (event.data.branch, event.data.sample)
            if run in outcomes:
                flag(event.id, f"second outcome for {run}")
            outcomes[run] = event.data
            finished.add(run)
    summary = next((event.data for event in events if isinstance(event, SummaryEvent)), None)
    if summary is None:
        return problems

    fix = sorted(sample for branch, sample in outcomes if branch == "fix")
    control = sorted(sample for branch, sample in outcomes if branch == "control")
    if fix != list(range(summary.k)):
        flag(None, f"expected fix outcomes for samples 0..{summary.k - 1}, got {fix}")
        return problems
    fix_rate = pass_rate(sum(outcomes[("fix", s)].passed for s in fix), summary.k)
    if abs(fix_rate.rate - summary.fix_rate) > 1e-9 or not _same_interval(fix_rate, summary.fix_ci):
        flag(None, "summary fix_rate/fix_ci disagree with the fix outcomes")
    if summary.preview:
        if control:
            flag(None, "preview summary but control outcomes were streamed")
        return problems
    if control != fix:
        flag(None, "control outcomes must cover the same samples as the fix outcomes")
        return problems
    control_rate = pass_rate(sum(outcomes[("control", s)].passed for s in control), summary.k)
    if summary.control_rate is None or summary.control_ci is None:
        flag(None, "summary is missing control values")
    elif abs(control_rate.rate - summary.control_rate) > 1e-9 or not _same_interval(
        control_rate, summary.control_ci
    ):
        flag(None, "summary control_rate/control_ci disagree with the control outcomes")
    if summary.verdict == "VERIFIED" and fix_rate.ci_low <= control_rate.ci_high:
        flag(None, "VERIFIED requires fix lower bound above control upper bound")
    if summary.verdict != "VERIFIED" and summary.k >= 3 and fix_rate.ci_low > control_rate.ci_high:
        flag(None, "bounds separate, so the verdict must be VERIFIED")
    return problems


def _same_interval(rate: PassRate, interval: tuple[float, float]) -> bool:
    return abs(rate.ci_low - interval[0]) <= 1e-6 and abs(rate.ci_high - interval[1]) <= 1e-6


# ---------------------------------------------------------------------------
# Forks, timeline, verification jobs
# ---------------------------------------------------------------------------


class Savings(ContractModel):
    reexecuted: Count
    cached: Count
    invalidated: Count
    total_steps: Count
    calls_reexecuted: Count
    calls_cached: Count
    tokens_saved: Count
    ms_saved: Millis


class ForkRunRef(ContractModel):
    run_id: RunId
    sample: Count
    branch: Branch
    passed: bool
    score: float | None
    reason: str | None


class ForkSummary(ContractModel):
    """GET /forks/{id}: an immutable named branch with its paired result."""

    fork_id: str
    base_run_id: RunId
    branch_name: str
    status: ForkStatus
    hypothesis: str | None
    edits: list[ForkEdit]
    mode: ReplayMode
    samples: int = Field(ge=1, le=10)
    control: bool
    created_at: AwareDatetime
    completed_at: AwareDatetime | None
    savings: Savings | None
    fix: PassRate | None
    control_result: PassRate | None
    verdict: Verdict | None
    preview: bool
    replay_fidelity: Fraction | None = Field(
        description="Share of pre-edit steps whose replayed state hash matched the recording."
    )
    edited_runs: list[ForkRunRef]
    control_runs: list[ForkRunRef]
    export_available: bool = Field(description="True only for a VERIFIED fork.")
    error: ErrorBody | None

    @model_validator(mode="after")
    def _verdict_rules(self) -> ForkSummary:
        if self.control_result is None and self.verdict is not None:
            raise ValueError("a fork without a paired control cannot have a verdict")
        if self.status == "complete" and self.preview != (self.control_result is None):
            raise ValueError("preview must be true exactly when there is no control result")
        if self.export_available and self.verdict != "VERIFIED":
            raise ValueError("export is only available for VERIFIED forks")
        if self.verdict == "VERIFIED" and self.fix is not None and self.control_result is not None:
            if self.fix.ci_low <= self.control_result.ci_high:
                raise ValueError("VERIFIED requires fix lower bound above control upper bound")
        return self


class TimelineEntry(ContractModel):
    """One node of the original -> control -> edited-fork timeline."""

    kind: TimelineKind
    label: str
    run_id: RunId | None
    fork_id: str | None
    parent_fork_id: str | None = Field(description="For a control entry: the fork it pairs with.")
    outcome: RunStatus | None
    score: float | None
    checker_reason: str | None
    pass_rate: PassRate | None
    verdict: Verdict | None
    preview: bool
    savings: Savings | None
    edit_summary: str | None


class ForkTimeline(ContractModel):
    """GET /runs/{id}/forks."""

    base_run_id: RunId
    entries: list[TimelineEntry]
    forks: list[ForkSummary]


class VerifyRequest(ContractModel):
    """POST /runs/{id}/verify. The verifier picks the fix for each candidate itself."""

    suspects: list[Addr] | None = Field(default=None, max_length=3)
    samples: int = Field(
        default=5,
        ge=4,
        le=10,
        description="K paired samples. Below 4, the Wilson intervals of K/K and 0/K always "
        "overlap, so no candidate could ever be VERIFIED.",
    )


class VerifyCandidate(ContractModel):
    addr: Addr
    status: JobStatus
    fork_id: str | None
    edit: ForkEdit | None
    edit_source: EditSource | None
    fix: PassRate | None
    control_result: PassRate | None
    verdict: Verdict | None
    note: str | None


class VerifyJob(ContractModel):
    """POST /runs/{id}/verify (202) and GET /jobs/{job_id}."""

    job_id: str
    run_id: RunId
    status: JobStatus
    samples: int = Field(ge=4, le=10)
    candidates: list[VerifyCandidate]
    best_addr: Addr | None = Field(description="Strongest VERIFIED candidate, if any.")
    created_at: AwareDatetime
    completed_at: AwareDatetime | None
    error: ErrorBody | None


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------


class Suspect(ContractModel):
    rank: int = Field(ge=1)
    addr: Addr
    name: str
    kind: StepKind
    score: float
    probability: Fraction = Field(
        description="Softmax over the run's step scores, temperature fitted."
    )
    in_conformal_set: bool
    flags: list[SuspectFlag]


class FeatureAttribution(ContractModel):
    name: str
    group: str = Field(description="Feature group, e.g. 'Lineage' or 'Validity'.")
    value: float | str | bool | None
    contribution: float = Field(description="Signed contribution to the suspect's score.")


class Reason(ContractModel):
    """One plain-language reason the model scored the suspect high."""

    suspect_addr: Addr
    text: str
    citation: Citation
    feature: FeatureAttribution


class DamageNode(ContractModel):
    addr: Addr
    name: str
    kind: StepKind
    tag: DamageTag
    is_visible_failure: bool


class DamagePath(ContractModel):
    """The suspect's forward cone. Every step appears once, tagged root/symptom/unaffected."""

    root_addr: Addr
    nodes: list[DamageNode]
    edges: list[Edge]
    reaches_final_answer: bool


class PrecedentCase(ContractModel):
    run_id: RunId
    root_addr: Addr
    fault_type: str | None
    similarity: Fraction
    fix_worked: bool | None
    label_source: LabelSource


class Precedents(ContractModel):
    """'Looks like 9 of 12 past stale-data failures; fresh=true fixed 7 of them.'"""

    fault_type: str | None
    neighbours: int = Field(ge=1)
    same_fault: Count
    fix_summary: str | None
    fix_tried: Count
    fix_worked: Count
    summary: str
    cases: list[PrecedentCase]

    @model_validator(mode="after")
    def _counts(self) -> Precedents:
        if not self.fix_worked <= self.fix_tried <= self.same_fault <= self.neighbours:
            raise ValueError("require fix_worked <= fix_tried <= same_fault <= neighbours")
        return self


class KnownGoodValue(ContractModel):
    citation: Citation
    value: Any | None
    failing_value: Any | None


class NearestTwin(ContractModel):
    """The nearest passing run of the same task or agent: a source of known-good values."""

    run: RunSummary
    similarity: Fraction
    same_task: bool
    basis: list[Literal["agent", "task_embedding", "graph_shape"]]
    known_good_values: list[KnownGoodValue]


class ProposedFix(ContractModel):
    """The *Propose* stage: a fix the Fork drawer is pre-filled with."""

    addr: Addr
    edit: ForkEdit
    source: EditSource
    rationale: str
    confidence: Fraction | None


class StageRail(ContractModel):
    localize: StageState
    attribute: StageState
    propose: StageState
    verify: StageState
    current: Stage


class Verification(ContractModel):
    """What the intervention says, kept separate from what the model suspects."""

    verdict: Verdict | None
    status: Literal["not_started", "running", "complete"]
    fork_id: str | None
    edit_addr: Addr | None
    k: int | None = Field(ge=1, le=10)
    fix: PassRate | None
    control: PassRate | None
    replay_fidelity: Fraction | None
    preview: bool
    explanation: str | None = Field(description="Why this verdict, or what would resolve it.")

    @model_validator(mode="after")
    def _rules(self) -> Verification:
        if self.verdict is not None and self.control is None:
            raise ValueError("a verdict needs a paired control")
        if self.control is None and self.fix is not None and not self.preview:
            raise ValueError("a result without a paired control must be flagged as a preview")
        if self.verdict == "VERIFIED" and self.fix is not None and self.control is not None:
            if self.fix.ci_low <= self.control.ci_high:
                raise ValueError("VERIFIED requires fix lower bound above control upper bound")
        if self.status == "not_started" and (self.fix or self.control or self.verdict):
            raise ValueError("a verification that has not started has no results")
        return self


class NarrativeClaim(ContractModel):
    text: str
    cites: list[Citation] = Field(min_length=1)


class Narrative(ContractModel):
    """Optional incident-style summary. Every claim cites an address that exists."""

    summary: str
    claims: list[NarrativeClaim]
    source: ClaimSource
    validated: bool


class Diagnosis(ContractModel):
    """GET /runs/{id}/diagnosis: ranked suspects, evidence, proof, and an honest abstain path."""

    run_id: RunId
    model_version: str
    computed_at: AwareDatetime
    latency_ms: Millis
    fixture: bool = Field(description="True for hand-authored illustrative content.")
    ranking: list[Suspect] = Field(min_length=1, description="Every step, most suspect first.")
    conformal_set: list[Addr]
    coverage_target: Fraction
    abstain: bool
    abstain_reason: AbstainReason | None
    abstain_message: str | None = Field(description="Headline shown instead of a culprit.")
    visible_failure_addr: Addr | None = Field(
        description="Where the failure showed up. Not necessarily the responsible step."
    )
    responsible_addr: Addr | None = Field(description="Top suspect; null when abstaining.")
    reasons: list[Reason]
    rule_evidence: list[RuleViolation]
    damage_path: DamagePath | None
    precedents: Precedents | None
    nearest_twin: NearestTwin | None
    proposed_fixes: list[ProposedFix]
    verification: Verification | None
    stages: StageRail
    narrative: Narrative | None

    @model_validator(mode="after")
    def _consistent(self) -> Diagnosis:
        addrs = [suspect.addr for suspect in self.ranking]
        if len(set(addrs)) != len(addrs):
            raise ValueError("ranking lists a step twice")
        if [suspect.rank for suspect in self.ranking] != list(range(1, len(addrs) + 1)):
            raise ValueError("ranks must run 1..n in order")
        probabilities = [suspect.probability for suspect in self.ranking]
        if probabilities != sorted(probabilities, reverse=True):
            raise ValueError("ranking must be sorted by probability, descending")
        if abs(sum(probabilities) - 1.0) > 1e-3:
            raise ValueError("step probabilities must sum to 1")
        unknown = set(self.conformal_set) - set(addrs)
        if unknown:
            raise ValueError(f"conformal_set names unknown steps: {sorted(unknown)}")
        if self.abstain:
            if self.abstain_reason is None or self.abstain_message is None:
                raise ValueError("abstaining needs abstain_reason and abstain_message")
            if self.responsible_addr is not None:
                raise ValueError("an abstaining diagnosis names no responsible step")
        else:
            if self.abstain_reason is not None or self.abstain_message is not None:
                raise ValueError("abstain_reason is only for abstaining diagnoses")
            if self.responsible_addr != addrs[0]:
                raise ValueError("responsible_addr must be the top-ranked step")
        for suspect in self.ranking:
            if suspect.in_conformal_set != (suspect.addr in self.conformal_set):
                raise ValueError("in_conformal_set disagrees with conformal_set")
        known = set(addrs)
        cited = [reason.citation.addr for reason in self.reasons]
        cited += [violation.citation.addr for violation in self.rule_evidence]
        if self.visible_failure_addr:
            cited.append(self.visible_failure_addr)
        if self.narrative:
            cited += [cite.addr for claim in self.narrative.claims for cite in claim.cites]
        if set(cited) - known:
            raise ValueError(f"citations name unknown steps: {sorted(set(cited) - known)}")
        return self


# ---------------------------------------------------------------------------
# Comparison (GET /diff)
# ---------------------------------------------------------------------------


class DiffSide(ContractModel):
    """One side of an aligned row. Payloads are omitted for same/cached rows."""

    seq: Count
    kind: StepKind
    name: str
    cache_status: CacheStatus
    input: Any | None
    output: Any | None
    state_writes: dict[str, Any]
    input_hash: str | None
    output_hash: str | None
    state_after_hash: str | None


class DiffRow(ContractModel):
    addr: Addr
    status: DiffStatus
    in_cone: bool
    changed: list[Literal["input", "output", "state"]]
    left: DiffSide | None
    right: DiffSide | None


class StateKeyDiff(ContractModel):
    key: str
    status: StateKeyStatus
    left: Any | None
    right: Any | None
    left_hash: str | None
    right_hash: str | None


class OutcomeBrief(ContractModel):
    status: RunStatus
    score: float | None
    reason: str | None


class OutcomeDiff(ContractModel):
    left: OutcomeBrief
    right: OutcomeBrief
    flipped: bool
    direction: Literal["fail_to_pass", "pass_to_fail", "unchanged"]


class DiffStats(ContractModel):
    same: Count
    cached: Count
    changed: Count
    new: Count
    removed: Count


class NearestPassingLink(ContractModel):
    run_id: RunId
    similarity: Fraction
    reason: str


class DiffResponse(ContractModel):
    """GET /diff?a=&b=. Rows are aligned by address, or by LCS when control flow changed."""

    left_run_id: RunId
    right_run_id: RunId
    alignment: AlignMode
    first_divergence: Addr | None
    stats: DiffStats
    rows: list[DiffRow]
    state_diff: list[StateKeyDiff]
    outcome: OutcomeDiff
    nearest_passing: NearestPassingLink | None

    @model_validator(mode="after")
    def _stats_match(self) -> DiffResponse:
        counted = {status: 0 for status in get_args(DiffStatus)}
        for row in self.rows:
            counted[row.status] += 1
        if counted != self.stats.model_dump():
            raise ValueError("stats must count the rows")
        if self.first_divergence is not None and self.first_divergence not in {
            row.addr for row in self.rows
        }:
            raise ValueError("first_divergence must be one of the rows")
        return self


# ---------------------------------------------------------------------------
# Regression export
# ---------------------------------------------------------------------------


class ExportedFile(ContractModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: Count


class ExportTestRequest(ContractModel):
    overwrite: bool = False


class ExportTestResponse(ContractModel):
    """POST /forks/{id}/export-test. Only a VERIFIED fork can be exported."""

    export_id: str
    fork_id: str
    verdict: Literal["VERIFIED"]
    test_path: str
    fixture_dir: str
    test_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: list[ExportedFile]
    invalidation_cone: list[Addr]
    run_command: str
    created_at: AwareDatetime


# ---------------------------------------------------------------------------
# Human labelling (for inter-annotator agreement)
# ---------------------------------------------------------------------------


class LabelRequest(ContractModel):
    """POST /labels. `root_addr` null means the annotator found no responsible step."""

    run_id: RunId
    annotator: str = Field(min_length=1, max_length=64)
    root_addr: Addr | None
    certainty: Literal["sure", "unsure"] = "sure"
    notes: str | None = Field(default=None, max_length=1000)


class LabelProgress(ContractModel):
    labelled: Count
    target: Count
    remaining: Count


class KappaStatus(ContractModel):
    kappa: float | None = Field(ge=-1, le=1)
    n: Count = Field(description="Runs labelled by at least two annotators.")
    annotators: Count


class LabelResponse(ContractModel):
    label_id: str
    run_id: RunId
    annotator: str
    root_addr: Addr | None
    created_at: AwareDatetime
    progress: LabelProgress
    agreement: KappaStatus


class LabelTask(ContractModel):
    """A failed run shown with the model's guess and the ground truth hidden."""

    run_id: RunId
    agent: str
    task: str
    checker_reason: str | None
    steps: Count
    labelled_by_you: bool


class LabelQueue(ContractModel):
    progress: LabelProgress
    items: list[LabelTask]


# ---------------------------------------------------------------------------
# OpenTelemetry ingest (POST /v1/traces)
# ---------------------------------------------------------------------------


class OtlpPartialSuccess(ContractModel):
    """OTLP/HTTP wire names (camelCase), so stock exporters accept the response."""

    rejectedSpans: Count
    errorMessage: str


class OtlpRunRef(ContractModel):
    run_id: RunId
    spans: Count


class OtlpIngestAck(ContractModel):
    """The OTLP ExportTraceServiceResponse plus what Black Box built from the spans."""

    partialSuccess: OtlpPartialSuccess | None
    accepted_spans: Count
    runs: list[OtlpRunRef]
    replayable: bool = Field(description="Always false: imported traces cannot be replayed.")
    note: str


# ---------------------------------------------------------------------------
# Crash report (GET /runs/{id}/report, /report.md)
# ---------------------------------------------------------------------------


class ReportClaim(ContractModel):
    text: str
    cites: list[Citation]


class ReportEvent(ContractModel):
    seq: Count
    addr: Addr
    text: str
    tag: DamageTag | None


class CrashReport(ContractModel):
    """Structured crash report; the Markdown export is rendered from the same content."""

    run_id: RunId
    title: str
    generated_at: AwareDatetime
    synopsis: str
    sequence_of_events: list[ReportEvent]
    probable_cause: ReportClaim | None
    contributing_factors: list[ReportClaim]
    findings: list[ReportClaim]
    recommended_fix: ReportClaim | None
    verification: ReportClaim | None
    fixture: bool


# ---------------------------------------------------------------------------
# Evaluation (GET /eval)
# ---------------------------------------------------------------------------


class Estimate(ContractModel):
    """A point estimate with an optional 95% interval (bootstrap over runs unless noted)."""

    value: float
    ci_low: float | None
    ci_high: float | None

    @model_validator(mode="after")
    def _ordered(self) -> Estimate:
        if (self.ci_low is None) != (self.ci_high is None):
            raise ValueError("ci_low and ci_high must both be set or both be null")
        if self.ci_low is not None and self.ci_high is not None and self.ci_low > self.ci_high:
            raise ValueError("ci_low must not exceed ci_high")
        return self


class PairedDifference(ContractModel):
    """Paired bootstrap difference (arm minus reference) over the same runs."""

    delta: float
    ci_low: float
    ci_high: float
    n: int = Field(ge=1)
    n_boot: int = Field(ge=1)
    excludes_zero: bool

    @model_validator(mode="after")
    def _consistent(self) -> PairedDifference:
        if self.ci_low > self.ci_high:
            raise ValueError("ci_low must not exceed ci_high")
        if self.excludes_zero != (self.ci_low > 0 or self.ci_high < 0):
            raise ValueError("excludes_zero disagrees with the interval")
        return self


class FeatureGroupInfo(ContractModel):
    id: str
    name: str
    n_features: Count
    enabled: bool


class DatasetCounts(ContractModel):
    base_runs: Count
    injected_forks: Count
    accepted_failures: Count
    natural_failures: Count
    human_labelled: Count
    runs_per_split: dict[str, Count]
    failures_per_family: dict[str, Count]


class DatasetInfo(ContractModel):
    version: str = Field(description="Hash of the frozen dataset (data/DATASET_VERSION).")
    frozen_at: AwareDatetime | None
    agents: list[str]
    held_out_fault_codes: list[str]
    counts: DatasetCounts


class ModelInfo(ContractModel):
    model_version: str
    backend: ModelBackend
    trained_at: AwareDatetime | None
    n_features: Count
    feature_groups: list[FeatureGroupInfo]
    temperature: float | None
    conformal_coverage: Fraction | None
    trained_on_runs: Count


class HeadlineCard(ContractModel):
    id: str
    title: str
    status: EvalStatus
    reason: str | None
    value: Estimate | None
    unit: Literal["fraction", "ms", "count"]
    n: int | None
    comparator_name: str | None
    comparator: Estimate | None
    tooltip: str


class LeaderboardRow(ContractModel):
    """One method on one split. Not-run and literature rows are explicit, never blank."""

    method: str
    method_kind: MethodKind
    split: Split
    status: EvalStatus
    reason: str | None
    reported_literature: bool = Field(
        description="A published number that this repository did not reproduce."
    )
    source: str | None
    n: int | None
    top1: Estimate | None
    top3: Estimate | None
    within1: Estimate | None
    mrr: Estimate | None
    latency_ms: Millis | None

    @model_validator(mode="after")
    def _status_rules(self) -> LeaderboardRow:
        metrics = [self.top1, self.top3, self.within1, self.mrr]
        if self.status == "ran":
            if self.reported_literature:
                raise ValueError("a row that ran cannot be a reported literature number")
            if self.n is None or self.n < 1 or any(metric is None for metric in metrics):
                raise ValueError("a row that ran needs n and every metric")
            if any(metric and metric.ci_low is None for metric in metrics):
                raise ValueError("a row that ran needs bootstrap intervals")
        else:
            if self.reason is None:
                raise ValueError("a row that did not run needs a reason")
            if not self.reported_literature and (self.n is not None or any(metrics)):
                raise ValueError("a not-run row cannot carry measured numbers")
            if self.reported_literature and self.source is None:
                raise ValueError("a literature row needs its source")
        return self


class MatrixCell(ContractModel):
    method: str
    split: Split
    value: float | None
    n: int | None
    status: EvalStatus
    reason: str | None


class GeneralisationMatrix(ContractModel):
    metric: Literal["top1"]
    methods: list[str]
    splits: list[Split]
    cells: list[MatrixCell]

    @model_validator(mode="after")
    def _complete(self) -> GeneralisationMatrix:
        expected = {(method, split) for method in self.methods for split in self.splits}
        if {(cell.method, cell.split) for cell in self.cells} != expected:
            raise ValueError("cells must cover every method x split exactly once")
        if len(self.cells) != len(expected):
            raise ValueError("duplicate matrix cells")
        return self


class FeatureGroupAblationRow(ContractModel):
    group_id: str
    group_name: str
    status: EvalStatus
    reason: str | None
    top1_without: Estimate | None
    delta_vs_full: PairedDifference | None


class FeatureGroupAblation(ContractModel):
    split: Split
    n: int | None
    full: Estimate | None
    rows: list[FeatureGroupAblationRow]


class AblationArm(ContractModel):
    arm: str
    label: str
    status: EvalStatus
    reason: str | None
    top1: Estimate | None
    diff_vs_reference: PairedDifference | None = Field(
        description="Arm minus reference on the same runs; null for the reference itself."
    )


class PairedAblation(ContractModel):
    """A recorder-value ablation. Reported even when the full record does not win."""

    id: Literal["provenance_edges", "telemetry_sufficiency"]
    title: str
    split: Split
    n: int | None
    reference_arm: str
    arms: list[AblationArm]
    interpretation: Literal["reference_wins", "no_significant_difference", "reference_loses"] | None

    @model_validator(mode="after")
    def _interpretation_follows_the_numbers(self) -> PairedAblation:
        """`reference_wins` only when every other arm is significantly worse."""
        if self.reference_arm not in {arm.arm for arm in self.arms}:
            raise ValueError("reference_arm must be one of the arms")
        others = [arm for arm in self.arms if arm.arm != self.reference_arm]
        for arm in self.arms:
            if arm.arm == self.reference_arm and arm.diff_vs_reference is not None:
                raise ValueError("the reference arm has no difference against itself")
        diffs = [arm.diff_vs_reference for arm in others]
        expected: str | None
        if not others or any(
            arm.status != "ran" or arm.diff_vs_reference is None for arm in others
        ):
            expected = None
        elif any(diff.ci_low > 0 for diff in diffs if diff):
            expected = "reference_loses"
        elif all(diff.ci_high < 0 for diff in diffs if diff):
            expected = "reference_wins"
        else:
            expected = "no_significant_difference"
        if self.interpretation != expected:
            raise ValueError(f"interpretation must be {expected!r} given the paired differences")
        return self


class IntegrityCheck(ContractModel):
    id: Literal["artifact_audit", "shuffled_labels", "human_agreement"]
    title: str
    status: EvalStatus
    reason: str | None
    value: Estimate | None
    threshold: float | None
    passes: bool | None
    n: int | None
    detail: str


class ReliabilityBin(ContractModel):
    lo: Fraction
    hi: Fraction
    n: Count
    mean_confidence: Fraction | None
    accuracy: Fraction | None


class AbstentionPoint(ContractModel):
    answered_fraction: Fraction
    top1_when_answered: Fraction | None
    n: Count


class Calibration(ContractModel):
    split: Split
    status: EvalStatus
    reason: str | None
    bins: list[ReliabilityBin]
    abstention: list[AbstentionPoint]
    ece: float | None
    conformal_target: Fraction | None
    conformal_empirical: Fraction | None
    mean_set_size: float | None
    abstain_rate: Fraction | None


class ModeSavings(ContractModel):
    mode: ReplayMode
    mean_steps_reexecuted: float
    mean_fraction_reexecuted: Fraction
    n: int = Field(ge=1)


class VerifierStats(ContractModel):
    n_known_roots: Count
    verified: Count
    refuted: Count
    inconclusive: Count
    n_wrong_roots: Count
    falsely_verified: Count


class ReplaySavings(ContractModel):
    status: EvalStatus
    reason: str | None
    n_forks: Count
    by_mode: list[ModeSavings]
    calls_saved_fraction: Estimate | None
    tokens_saved: Count
    ms_saved: Millis
    verifier: VerifierStats | None


class DetectorMetrics(ContractModel):
    """Run-level failure detector: powers the risk column in the Runs table."""

    status: EvalStatus
    reason: str | None
    split: Split | None
    n_runs: Count
    n_failed: Count
    auroc: Estimate | None
    accuracy: Estimate | None


class NotRunItem(ContractModel):
    section: str
    item: str
    reason: str


class EvalSummary(ContractModel):
    """Headline numbers written by ``python -m blackbox.ml eval`` to ``data/eval/summary.json``."""

    sample_size: int = Field(ge=0)
    metrics: dict[str, float]
    ablations: dict[str, dict[str, float]]


class EvalResponse(ContractModel):
    """GET /eval: every table the Results page draws. Numbers always carry n and an interval."""

    fixture: bool
    fixture_notice: str | None = Field(
        description="Shown prominently when fixture is true; null otherwise."
    )
    generated_at: AwareDatetime
    dataset: DatasetInfo
    model: ModelInfo
    headline: list[HeadlineCard]
    leaderboard: list[LeaderboardRow]
    generalisation: GeneralisationMatrix
    feature_group_ablation: FeatureGroupAblation
    recorder_ablations: list[PairedAblation]
    integrity: list[IntegrityCheck]
    calibration: Calibration
    replay_savings: ReplaySavings
    detector: DetectorMetrics
    not_run: list[NotRunItem]
    notes: list[str]

    @model_validator(mode="after")
    def _fixture_flag(self) -> EvalResponse:
        if self.fixture and not self.fixture_notice:
            raise ValueError("a fixture must carry a fixture_notice")
        if not self.fixture and self.fixture_notice is not None:
            raise ValueError("fixture_notice is only for fixtures")
        return self


# ---------------------------------------------------------------------------
# Endpoint table
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Endpoint:
    method: str
    path: str
    summary: str
    request: str | None
    response: str
    statuses: tuple[int, ...]
    query: tuple[str, ...] = ()
    extension: bool = False


_JSON_ERRORS = (404, 422, 500)
ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint(
        "GET", "/health", "Liveness, mode and capabilities", None, "AppHealth", (200,), (), True
    ),
    Endpoint("GET", "/agents", "Agents with run counts", None, "AgentList", (200,), (), True),
    Endpoint(
        "POST",
        "/tasks/run",
        "Record an offline TripCrew task from a bounded natural-language prompt",
        "TaskRunRequest",
        "TaskRunResponse",
        (201, 409, 422, 503),
        (),
        True,
    ),
    Endpoint(
        "GET",
        "/runs",
        "Paged run list",
        "RunListQuery",
        "RunList",
        (200, 422),
        (
            "agent",
            "outcome",
            "origin",
            "split",
            "label_source",
            "failure_signature",
            "q",
            "sort",
            "order",
            "limit",
            "offset",
        ),
    ),
    Endpoint(
        "GET",
        "/failure-groups",
        "Failed runs grouped by signature",
        None,
        "FailureGroupList",
        (200,),
        ("agent", "split"),
        True,
    ),
    Endpoint(
        "GET",
        "/runs/{run_id}",
        "Run detail with steps and edges",
        None,
        "RunDetail",
        (200, 404),
        ("blind",),
    ),
    Endpoint(
        "GET",
        "/runs/{run_id}/steps/{addr}",
        "One step with full payloads",
        None,
        "StepDetail",
        (200, 404),
    ),
    Endpoint(
        "GET",
        "/runs/{run_id}/provenance",
        "Producer candidates and consumer path of one value",
        None,
        "ValueProvenance",
        (200, 404, 422),
        ("addr", "pointer"),
    ),
    Endpoint(
        "GET",
        "/runs/{run_id}/diagnosis",
        "Cached diagnosis, computed on first request",
        None,
        "Diagnosis",
        (200, 404, 409, 503),
        ("refresh",),
    ),
    Endpoint(
        "POST",
        "/runs/{run_id}/verify",
        "Start the paired-replay verifier",
        "VerifyRequest",
        "VerifyJob",
        (202, 404, 409, 422, 429),
    ),
    Endpoint(
        "GET", "/jobs/{job_id}", "Poll a verification job", None, "VerifyJob", (200, 404), (), True
    ),
    Endpoint(
        "POST",
        "/replay/predict",
        "Predict what a fork will re-run",
        "ForkRequest",
        "ReplayPrediction",
        (200, 404, 422),
        (),
        True,
    ),
    Endpoint(
        "POST",
        "/forks",
        "Create an immutable fork and start replaying",
        "ForkRequest",
        "ForkCreated",
        (202, 404, 409, 422, 429),
    ),
    Endpoint(
        "GET",
        "/forks/{fork_id}",
        "One fork with its paired result",
        None,
        "ForkSummary",
        (200, 404),
        (),
        True,
    ),
    Endpoint(
        "GET",
        "/forks/{fork_id}/stream",
        "Server-sent events of one fork",
        None,
        "text/event-stream",
        (200, 404),
        (),
        False,
    ),
    Endpoint(
        "GET",
        "/runs/{run_id}/forks",
        "Original -> control -> fork timeline",
        None,
        "ForkTimeline",
        (200, 404),
        (),
        True,
    ),
    Endpoint(
        "GET",
        "/diff",
        "Aligned comparison of two runs",
        None,
        "DiffResponse",
        (200, 404, 422),
        ("a", "b"),
    ),
    Endpoint(
        "GET",
        "/runs/{run_id}/twin",
        "Nearest passing twin",
        None,
        "NearestTwin",
        (200, 404),
        (),
        True,
    ),
    Endpoint(
        "GET",
        "/runs/{run_id}/report",
        "Structured crash report",
        None,
        "CrashReport",
        (200, 404, 409),
        (),
        True,
    ),
    Endpoint(
        "GET",
        "/runs/{run_id}/report.md",
        "Crash report as Markdown",
        None,
        "text/markdown",
        (200, 404, 409),
        (),
        True,
    ),
    Endpoint(
        "POST",
        "/forks/{fork_id}/export-test",
        "Generate an offline regression test",
        "ExportTestRequest",
        "ExportTestResponse",
        (200, 404, 409),
    ),
    Endpoint("GET", "/eval", "Precomputed evaluation", None, "EvalResponse", (200, 404)),
    Endpoint(
        "GET",
        "/labels/queue",
        "Failed runs awaiting a human label",
        None,
        "LabelQueue",
        (200,),
        ("annotator", "limit"),
        True,
    ),
    Endpoint(
        "POST",
        "/labels",
        "Record a human label",
        "LabelRequest",
        "LabelResponse",
        (201, 404, 409, 422),
    ),
    Endpoint(
        "POST",
        "/v1/traces",
        "OTLP/HTTP JSON trace ingest",
        "OTLP ExportTraceServiceRequest",
        "OtlpIngestAck",
        (200, 400, 422),
    ),
)


# Named unions exported to TypeScript as type aliases.
UNIONS: dict[str, tuple[type[BaseModel], ...]] = {
    "ForkEvent": (StepEvent, OutcomeEvent, SummaryEvent, StreamErrorEvent),
}

# Models exported to the JSON Schema bundle, in a stable order.
EXPORTED_MODELS: tuple[type[BaseModel], ...] = (
    ErrorResponse,
    ErrorBody,
    ValidationIssue,
    AppHealth,
    AgentList,
    TaskRunRequest,
    TaskRunResponse,
    RunListQuery,
    RunList,
    RunSummary,
    FailureGroupList,
    RunDetail,
    StepDetail,
    ValueProvenance,
    Diagnosis,
    VerifyRequest,
    VerifyJob,
    ForkRequest,
    ForkCreated,
    ReplayPrediction,
    ForkSummary,
    ForkTimeline,
    ForkStreamRecording,
    StepEvent,
    OutcomeEvent,
    SummaryEvent,
    StreamErrorEvent,
    DiffResponse,
    NearestTwin,
    CrashReport,
    ExportTestRequest,
    ExportTestResponse,
    LabelRequest,
    LabelResponse,
    LabelQueue,
    OtlpIngestAck,
    EvalResponse,
)
