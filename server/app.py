"""FastAPI adapter for the Black Box service layer."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agents.tripcrew import TravelAPI, TripCrew
from agents.tripcrew.prompt import catalog_seed, parse_trip_prompt
from blackbox.api.errors import ApiError
from blackbox.api.reader import build_diff
from blackbox.api.service import BlackBoxService
from server import models as m

logger = logging.getLogger("blackbox.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    has_recordings = data_dir.is_dir() and any(
        (child / "blackbox.db").is_file() for child in data_dir.iterdir()
    )
    service = BlackBoxService(data_root=data_dir, static_bundle=not has_recordings)
    app.state.service = service
    # Risk, top suspect and failure groups come from the index; score it without blocking.
    service.warm_in_background()
    try:
        yield
    finally:
        service.close()


app = FastAPI(title="Black Box API", version=m.CONTRACT_VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    # Browsers send origins without a trailing slash; tolerate one in the setting.
    allow_origins=[
        origin.strip().rstrip("/")
        for origin in os.environ.get(
            "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
        ).split(",")
        if origin.strip()
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Last-Event-ID"],
)


def service(request: Request) -> BlackBoxService:
    return request.app.state.service


def require_recordings(svc: BlackBoxService) -> None:
    if svc.static_bundle:
        raise ApiError(
            "unavailable",
            "The recorded API bundle has no dataset attached.",
            hint="The web client will show its labelled read-only demo fixtures.",
        )


def _error(error: ApiError, request_id: str | None = None) -> Response:
    body = m.ErrorResponse(error=error.body(request_id))
    return Response(body.model_dump_json(), status_code=error.status, media_type="application/json")


@app.middleware("http")
async def request_id(request: Request, call_next):
    request.state.request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, error: ApiError):
    return _error(error, getattr(request.state, "request_id", None))


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, error: StarletteHTTPException):
    # Unknown routes and wrong methods use the same error envelope as every other failure.
    code = {404: "not_found", 405: "unsupported", 400: "bad_request"}.get(
        error.status_code, "internal_error"
    )
    message = {
        404: f"No API route matches {request.method} {request.url.path}.",
        405: f"{request.method} is not supported on {request.url.path}.",
    }.get(error.status_code, str(error.detail))
    return _error(
        ApiError(code, message, status=error.status_code, hint="See /docs for the API routes."),
        getattr(request.state, "request_id", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, error: RequestValidationError):
    issues = [
        m.ValidationIssue(loc=list(i["loc"]), message=i["msg"], type=i["type"])
        for i in error.errors()
    ]
    body = m.ErrorResponse(
        error=m.ErrorBody(
            code="validation_error",
            message="The request did not match the API contract.",
            status=422,
            hint="Check the field locations in issues.",
            issues=issues,
            context=None,
            request_id=getattr(request.state, "request_id", None),
        )
    )
    return Response(body.model_dump_json(), status_code=422, media_type="application/json")


@app.exception_handler(Exception)
async def internal_error_handler(request: Request, error: Exception):
    logger.exception("Unhandled API error", exc_info=error)
    return _error(
        ApiError("internal_error", "The API could not complete this request."),
        getattr(request.state, "request_id", None),
    )


@app.get("/health", response_model=m.AppHealth)
def health(request: Request):
    svc = service(request)
    result = svc.health()
    if not svc.static_bundle:
        return result
    capabilities = result.capabilities.model_copy(
        update={
            "fork": False,
            "verify": False,
            "export_test": False,
            "label": False,
            "ingest_otlp": False,
        }
    )
    return result.model_copy(
        update={
            "static_bundle": True,
            "notes": [
                *result.notes,
                "No recorded database is attached; the web app uses read-only fixture data.",
            ],
            "capabilities": capabilities,
        }
    )


@app.get("/agents", response_model=m.AgentList)
def agents(request: Request):
    svc = service(request)
    require_recordings(svc)
    return svc.list_agents()


@app.post("/tasks/run", response_model=m.TaskRunResponse, status_code=201)
async def run_task(request: Request, body: m.TaskRunRequest):
    """Execute a supported natural-language travel task with the local recorded agent."""
    svc = service(request)
    require_recordings(svc)
    if svc.mode != "offline":
        raise ApiError(
            "unsupported",
            "Prompt runs are available in offline mode only.",
            hint="Set MODE=offline and restart the API to use the deterministic demo runner.",
        )
    agent_data = svc.agents.get("tripcrew")
    if agent_data is None:
        raise ApiError("unavailable", "The TripCrew recorder is not available.")

    # Each run keeps a unique task id (its replay metadata is stored per run), but the
    # synthetic catalog is seeded from the request constraints. Budget amounts are excluded,
    # so changing the budget does not silently change the underlying prices.
    task_id = f"PROMPT-{uuid.uuid4().hex[:12].upper()}"
    scenario_id, seed = catalog_seed(body.prompt)
    try:
        scenario = parse_trip_prompt(body.prompt, scenario_id=scenario_id, seed=seed)
    except ValueError as error:
        raise ApiError("bad_request", str(error)) from error

    metadata_dir = agent_data.recorder.data_dir / "prompt-scenarios"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = metadata_dir / f"{task_id}.json"
    temporary_path = metadata_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(
            {"scenario": asdict(scenario), "stale_fx": body.inject_stale_fx, "prompt": body.prompt}
        ),
        encoding="utf-8",
    )
    temporary_path.replace(metadata_path)

    agent = TripCrew(
        scenario,
        TravelAPI([scenario], stale_fx=body.inject_stale_fx),
        model="tripcrew-fixture-v1",
        task_prompt=body.prompt,
    )
    recorded = agent_data.recorder.run(
        "tripcrew",
        task_id,
        seed,
        model="tripcrew-fixture-v1",
    )
    with recorded:
        try:
            await agent(recorded)
        except (
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            AttributeError,
            ArithmeticError,
        ) as error:
            recorded.set_outcome(
                False, score=0, reason=f"Agent raised {type(error).__name__}: {error}"
            )

    svc.ensure_index()
    svc.warm(run_ids=[recorded.run_id])
    svc.cache.details.clear()
    detail = svc.run_detail(recorded.run_id)
    return m.TaskRunResponse(
        run_id=recorded.run_id,
        status=detail.run.status,
        task=detail.run.task,
        steps=len(detail.steps),
    )


@app.get("/runs", response_model=m.RunList)
def runs(request: Request, query: m.RunListQuery = Depends()):
    svc = service(request)
    require_recordings(svc)
    return svc.list_runs(query)


@app.get("/failure-groups", response_model=m.FailureGroupList)
def failure_groups(request: Request, agent: str | None = None, split: str | None = None):
    svc = service(request)
    require_recordings(svc)
    return svc.failure_groups(agent, split)


@app.get("/runs/{run_id}", response_model=m.RunDetail)
def run_detail(request: Request, run_id: str, blind: bool = False):
    svc = service(request)
    require_recordings(svc)
    return svc.run_detail(run_id, blind=blind)


# Step addresses contain "/" (e.g. "fx/tool#1"), so the parameter must accept a path.
@app.get("/runs/{run_id}/steps/{addr:path}", response_model=m.StepDetail)
def step(request: Request, run_id: str, addr: str):
    return service(request).step(run_id, addr)


@app.get("/runs/{run_id}/provenance", response_model=m.ValueProvenance)
def provenance(request: Request, run_id: str, addr: str, pointer: str):
    return service(request).provenance(run_id, addr, pointer)


@app.get("/runs/{run_id}/diagnosis", response_model=m.Diagnosis)
def diagnosis(request: Request, run_id: str, refresh: bool = False):
    return service(request).diagnosis(run_id, refresh=refresh)


@app.post("/replay/predict", response_model=m.ReplayPrediction)
def replay_predict(request: Request, body: m.ForkRequest):
    svc = service(request)
    require_recordings(svc)
    return svc.forks.predict(body)


@app.post("/forks", response_model=m.ForkCreated, status_code=202)
async def create_fork(request: Request, body: m.ForkRequest):
    svc = service(request)
    require_recordings(svc)
    return await svc.forks.create(body)


@app.get("/forks/{fork_id}/stream")
async def fork_stream(request: Request, fork_id: str):
    svc = service(request)
    # Validate before returning headers; streaming errors cannot become JSON responses.
    svc.forks._meta(fork_id)

    async def events():
        async for item in svc.forks.stream(fork_id):
            if await request.is_disconnected():
                break
            event = item if isinstance(item, dict) else item.model_dump(mode="json")
            yield f"id: {event['id']}\nevent: {event['event']}\ndata: {json.dumps(event['data'], separators=(',', ':'))}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/forks/{fork_id}", response_model=m.ForkSummary)
def fork_summary(request: Request, fork_id: str):
    return service(request).forks.summary(fork_id)


@app.get("/runs/{run_id}/forks", response_model=m.ForkTimeline)
def fork_timeline(request: Request, run_id: str):
    return service(request).forks.timeline(run_id)


@app.post("/runs/{run_id}/verify", response_model=m.VerifyJob, status_code=202)
async def verify(request: Request, run_id: str, body: m.VerifyRequest):
    svc = service(request)
    require_recordings(svc)
    return await svc.forks.start_verify(run_id, body)


@app.get("/jobs/{job_id}", response_model=m.VerifyJob)
def verify_job(request: Request, job_id: str):
    return service(request).forks.get_job(job_id)


@app.get("/diff", response_model=m.DiffResponse)
def diff(request: Request, a: str, b: str):
    svc = service(request)
    left, right = svc.run_detail(a), svc.run_detail(b)
    if svc.agent_of(a) is not svc.agent_of(b):
        raise ApiError("validation_error", "Runs from different agents cannot be aligned.")
    nearest = None
    if left.run.status == "failed" and right.run.status == "passed":
        nearest = m.NearestPassingLink(
            run_id=b,
            similarity=1.0,
            reason="Compared directly",
        )
    return build_diff(left, right, invalidated=None, nearest=nearest)


@app.get("/runs/{run_id}/twin", response_model=m.NearestTwin)
def twin(request: Request, run_id: str):
    return service(request).twin(run_id)


@app.get("/eval", response_model=m.EvalResponse)
def evaluation(request: Request):
    svc = service(request)
    path = svc.eval_dir / "api.json"
    if path.is_file():
        return m.EvalResponse.model_validate_json(path.read_text(encoding="utf-8"))
    try:
        from blackbox.api.eval_adapter import build_eval

        return build_eval(svc)
    except FileNotFoundError:
        pass
    fixture = Path(__file__).parent.parent / "web" / "mocks" / "eval.json"
    if fixture.is_file():
        return m.EvalResponse.model_validate_json(fixture.read_text(encoding="utf-8"))
    raise ApiError("unavailable", "Evaluation report has not been generated.")


@app.get("/runs/{run_id}/report", response_model=m.CrashReport)
def crash_report(request: Request, run_id: str):
    svc = service(request)
    detail = svc.run_detail(run_id)
    if detail.run.status != "failed":
        raise ApiError("not_applicable", "Crash reports are only available for failed runs.")
    try:
        diagnosis = svc.diagnosis(run_id)
    except ApiError as error:
        if error.status >= 500:
            diagnosis = None
        else:
            raise
    suspect = diagnosis.responsible_addr if diagnosis else None
    # When the model abstains, the report still names its leading candidate, labelled as such.
    leading = suspect or (diagnosis.ranking[0].addr if diagnosis and diagnosis.ranking else None)
    names = {s.addr: s.name for s in detail.steps}
    reason = detail.run.checker_reason or "The run failed its task checks."
    events = [
        m.ReportEvent(
            seq=s.seq,
            addr=s.addr,
            text=f"{s.name}: {('error: ' + s.error.type) if s.error else ('visible failure' if diagnosis and s.addr == diagnosis.visible_failure_addr else 'recorded step')}",
            tag="root"
            if s.addr == leading
            else "symptom"
            if diagnosis and s.addr == diagnosis.visible_failure_addr
            else None,
        )
        for s in detail.steps
    ]
    if suspect:
        cause_text = f"The most likely cause is {names.get(suspect, suspect)} ({suspect}). {reason}"
    elif leading:
        probability = diagnosis.ranking[0].probability if diagnosis else 0.0
        cause_text = (
            f"No single step is certain; the leading candidate is "
            f"{names.get(leading, leading)} ({leading}, {probability:.0%}). {reason}"
        )
    else:
        cause_text = reason
    cause = m.ReportClaim(
        text=cause_text,
        cites=[m.Citation(addr=leading, json_pointer=None)] if leading else [],
    )
    contributing: list[m.ReportClaim] = []
    recommended = None
    verification = None
    if diagnosis:
        # Other candidates the model could not rule out (its conformal set).
        for addr in diagnosis.conformal_set:
            if addr == leading or addr not in names:
                continue
            p = next((s.probability for s in diagnosis.ranking if s.addr == addr), 0.0)
            contributing.append(
                m.ReportClaim(
                    text=f"{names[addr]} ({addr}) remains a candidate at {p:.0%}.",
                    cites=[m.Citation(addr=addr, json_pointer=None)],
                )
            )
            if len(contributing) == 3:
                break
        if diagnosis.damage_path and diagnosis.damage_path.reaches_final_answer:
            contributing.append(
                m.ReportClaim(
                    text=f"The value from {diagnosis.damage_path.root_addr} flows into the final "
                    "answer, so the error propagated instead of being caught.",
                    cites=[m.Citation(addr=diagnosis.damage_path.root_addr, json_pointer=None)],
                )
            )
        fix = next((f for f in diagnosis.proposed_fixes if f.addr == leading), None) or (
            diagnosis.proposed_fixes[0] if diagnosis.proposed_fixes else None
        )
        if fix:
            recommended = m.ReportClaim(
                text=f"{fix.rationale} ({fix.edit.kind.replace('_', ' ')} at {fix.addr}, "
                f"source: {fix.source.replace('_', ' ')}).",
                cites=[m.Citation(addr=fix.addr, json_pointer=None)],
            )
        if diagnosis.verification and diagnosis.verification.explanation:
            edit_addr = diagnosis.verification.edit_addr
            verification = m.ReportClaim(
                text=diagnosis.verification.explanation,
                cites=[m.Citation(addr=edit_addr, json_pointer=None)] if edit_addr else [],
            )
    return m.CrashReport(
        run_id=run_id,
        title=f"Failure report · {detail.run.task}",
        generated_at=datetime.now(UTC),
        synopsis=f"{detail.run.agent} failed: {reason}",
        sequence_of_events=events,
        probable_cause=cause,
        contributing_factors=contributing,
        findings=(
            [
                m.ReportClaim(text=reason.text, cites=[reason.citation])
                for reason in diagnosis.reasons
            ]
            if diagnosis
            else []
        ),
        recommended_fix=recommended,
        verification=verification,
        fixture=False,
    )


@app.get("/runs/{run_id}/report.md")
def crash_report_markdown(request: Request, run_id: str):
    report = crash_report(request, run_id)
    lines = [f"# {report.title}", "", report.synopsis, "", "## Event sequence", ""]
    lines.extend(f"- `{e.addr}` — {e.text}" for e in report.sequence_of_events)
    if report.probable_cause:
        lines.extend(["", "## Probable cause", "", report.probable_cause.text])
    if report.contributing_factors:
        lines.extend(["", "## Contributing factors", ""])
        lines.extend(f"- {c.text}" for c in report.contributing_factors)
    if report.findings:
        lines.extend(["", "## Evidence", ""])
        lines.extend(f"- {f.text}" for f in report.findings)
    if report.recommended_fix:
        lines.extend(["", "## Recommended fix", "", report.recommended_fix.text])
    if report.verification:
        lines.extend(["", "## Verification", "", report.verification.text])
    return Response(
        "\n".join(lines) + "\n",
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="blackbox-{run_id}.md"'},
    )


@app.post("/forks/{fork_id}/export-test", response_model=m.ExportTestResponse)
def export_test(request: Request, fork_id: str, body: m.ExportTestRequest):
    svc = service(request)
    require_recordings(svc)
    from blackbox.api.exports import export_verified

    return export_verified(svc, fork_id, overwrite=body.overwrite)


@app.get("/labels/queue", response_model=m.LabelQueue)
def label_queue(
    request: Request, annotator: str = "anonymous", limit: int = Query(50, ge=1, le=200)
):
    svc = service(request)
    require_recordings(svc)
    runs = [r for r in svc._all_rows() if r["outcome"] == "failed" and r["origin"] != "fork"]
    labelled = {
        r["run_id"]
        for a in svc.agents.values()
        for r in a.database.query("SELECT run_id FROM human_labels WHERE annotator=?", (annotator,))
    }
    items = []
    for row in runs:
        if row["run_id"] in labelled:
            continue
        items.append(
            m.LabelTask(
                run_id=row["run_id"],
                agent=row["agent"],
                task=row["task"],
                checker_reason=row["checker_reason"],
                steps=row["n_steps"],
                labelled_by_you=False,
            )
        )
        if len(items) >= limit:
            break
    target = min(50, len(runs))
    return m.LabelQueue(
        progress=m.LabelProgress(
            labelled=len(labelled), target=target, remaining=max(0, target - len(labelled))
        ),
        items=items,
    )


@app.post("/labels", response_model=m.LabelResponse, status_code=201)
def save_label(request: Request, body: m.LabelRequest):
    svc = service(request)
    require_recordings(svc)
    detail = svc.run_detail(body.run_id)
    if body.root_addr and body.root_addr not in {s.addr for s in detail.steps}:
        raise ApiError("validation_error", f"Run has no step {body.root_addr!r}.")
    agent = svc.agent_of(body.run_id)
    label_id = uuid.uuid4().hex
    created = datetime.now(UTC)
    try:
        agent.database.execute(
            "INSERT INTO human_labels(label_id,run_id,annotator,root_addr,certainty,notes,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                label_id,
                body.run_id,
                body.annotator,
                body.root_addr,
                body.certainty,
                body.notes,
                created.isoformat(),
            ),
        )
    except sqlite3.IntegrityError as error:
        raise ApiError("conflict", "This annotator already labelled the run.") from error
    all_labels = [
        row
        for data in svc.agents.values()
        for row in data.database.query("SELECT run_id, annotator, root_addr FROM human_labels")
    ]
    by_annotator: dict[str, dict[str, str]] = {}
    for row in all_labels:
        by_annotator.setdefault(row["annotator"], {})[row["run_id"]] = (
            row["root_addr"] or "__no_responsible_step__"
        )
    annotators = sorted(by_annotator)
    pairwise: list[tuple[float, int]] = []
    double_labelled_runs: set[str] = set()
    for index, left in enumerate(annotators):
        for right in annotators[index + 1 :]:
            left_labels, right_labels = by_annotator[left], by_annotator[right]
            overlap = set(left_labels) & set(right_labels)
            if not overlap:
                continue
            double_labelled_runs.update(overlap)
            observed = sum(left_labels[run] == right_labels[run] for run in overlap) / len(overlap)
            categories = set(left_labels[run] for run in overlap) | set(
                right_labels[run] for run in overlap
            )
            expected = sum(
                sum(left_labels[run] == category for run in overlap)
                * sum(right_labels[run] == category for run in overlap)
                for category in categories
            ) / (len(overlap) ** 2)
            kappa = (observed - expected) / (1 - expected) if expected < 1 else 1.0
            pairwise.append((kappa, len(overlap)))
    weighted = sum(value * count for value, count in pairwise)
    overlap_n = sum(count for _, count in pairwise)
    agreement = m.KappaStatus(
        kappa=weighted / overlap_n if overlap_n else None,
        n=len(double_labelled_runs),
        annotators=len(annotators),
    )
    total = sum(r["outcome"] == "failed" and r["origin"] != "fork" for r in svc._all_rows())
    count = len(
        {
            r["run_id"]
            for a in svc.agents.values()
            for r in a.database.query(
                "SELECT run_id FROM human_labels WHERE annotator=?", (body.annotator,)
            )
        }
    )
    return m.LabelResponse(
        label_id=label_id,
        run_id=body.run_id,
        annotator=body.annotator,
        root_addr=body.root_addr,
        created_at=created,
        progress=m.LabelProgress(
            labelled=count, target=min(50, total), remaining=max(0, min(50, total) - count)
        ),
        agreement=agreement,
    )


@app.post("/v1/traces", response_model=m.OtlpIngestAck)
async def ingest_otlp(request: Request):
    # OTLP is accepted in the standard path. Each resource/scope span is turned into an
    # imported, non-replayable trace by the dedicated importer.
    svc = service(request)
    require_recordings(svc)
    try:
        payload = await request.json()
    except ValueError as error:
        raise ApiError(
            "bad_request",
            "The request body is not valid JSON.",
            hint="Send OTLP/HTTP JSON (Content-Type: application/json), not protobuf.",
        ) from error
    if not isinstance(payload, dict):
        raise ApiError("bad_request", "An OTLP export request must be a JSON object.")
    from blackbox.api.otlp import ingest

    return ingest(svc, payload)
