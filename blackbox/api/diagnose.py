"""Turn the explain layer's report into the contract ``Diagnosis``.

Evidence pointers from :mod:`blackbox.explain.evidence` are relative to a step's semantic
payload (parsed LLM content, tool arguments). The API cites fields of the *step document*
(``{input, output, reasoning, state_before, state_after}``), so every pointer is mapped
onto the recorded payload here and checked to resolve before it is served.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from blackbox.api.index import FAULT_SPECS
from blackbox.api.reader import damage_path, resolve_pointer, step_document
from blackbox.explain.evidence import Finding, leaves
from blackbox.explain.reasons import GROUP_NAMES
from blackbox.explain.report import DiagnosisReport, is_evaluation_step
from blackbox.ml.features import output_payload
from blackbox.ml.model import Diagnoser
from server import models as m

TOP_REASONED = 3
MAX_KNOWN_GOOD = 6

FINDING_RULES: dict[str, tuple[m.SchemaRule, m.Severity]] = {
    "parse_failed": ("schema_invalid", "error"),
    "error_payload": ("error_status_in_output", "error"),
    "exception": ("other", "error"),
    "empty_output": ("empty_result", "warning"),
    "stale_date": ("stale_as_of", "error"),
    "value_outlier": ("non_finite_or_out_of_range", "warning"),
    "type_mismatch": ("schema_invalid", "warning"),
    "unseen_field": ("schema_invalid", "info"),
    "missing_field": ("schema_invalid", "warning"),
    "conflict_input": ("other", "warning"),
    "conflict_earlier": ("other", "warning"),
    "empty_argument": ("empty_query", "warning"),
}


# ---------------------------------------------------------------------------
# Pointers
# ---------------------------------------------------------------------------


def _last_user_index(step: m.StepDetail) -> int | None:
    messages = step.input.get("messages") if isinstance(step.input, dict) else None
    if not isinstance(messages, list):
        return None
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], dict) and messages[index].get("role") == "user":
            return index
    return None


def document_pointer(step: m.StepDetail, pointer: str | None) -> str | None:
    """Map a semantic-payload pointer onto the step document, to the deepest part that exists."""
    if pointer is None:
        return None
    mapped: str | None = pointer
    if pointer.startswith("/output"):
        rest = pointer[len("/output") :]
        if step.kind == "llm":
            mapped = "/output/choices/0/message/content" + rest
        elif step.kind == "state":
            mapped = "/state_after" + rest
    elif pointer.startswith("/input"):
        rest = pointer[len("/input") :]
        if step.kind == "llm":
            index = _last_user_index(step)
            mapped = f"/input/messages/{index}/content{rest}" if index is not None else "/input"
        elif isinstance(step.input, dict) and "args" in step.input:
            mapped = "/input/args" + rest
        elif step.kind == "state":
            mapped = None
    if mapped is None:
        return None
    document = step_document(step, "src")
    parts = mapped.split("/")
    while len(parts) > 1:
        candidate = "/".join(parts)
        if resolve_pointer(document, candidate)[0]:
            return candidate
        parts.pop()
    return None


def citation(steps: dict[str, m.StepDetail], addr: str, pointer: str | None) -> m.Citation:
    step = steps.get(addr)
    return m.Citation(addr=addr, json_pointer=document_pointer(step, pointer) if step else None)


def rule_from_finding(steps: dict[str, m.StepDetail], finding: Finding) -> m.RuleViolation:
    rule, severity = FINDING_RULES.get(finding.code, ("other", "info"))
    related = [
        {"addr": c.addr, "json_pointer": citation(steps, c.addr, c.pointer).json_pointer}
        for c in finding.related
    ]
    return m.RuleViolation(
        rule=rule,
        severity=severity,
        message=finding.message,
        citation=citation(steps, finding.citation.addr, finding.citation.pointer),
        evidence={"finding": finding.code, "related": related}
        if related
        else {"finding": finding.code},
    )


# ---------------------------------------------------------------------------
# Twin and fixes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Twin:
    summary: m.RunSummary
    steps: dict[str, m.StepDetail]
    similarity: float
    same_task: bool
    basis: list[str]


def _semantic_output(step: m.StepDetail) -> Any:
    if step.kind == "state":
        keys = [write.split("@")[0] for write in step.writes]
        return {key: step.state_after.get(key) for key in keys} or None
    from blackbox.ml.dataset import TraceStep

    trace_step = TraceStep(
        step.addr, step.seq, step.kind, step.name, step.agent_role, step.input, step.output,
        tuple(step.reads), tuple(step.writes), step.finish_reason, None, step.retries,
    )  # fmt: skip
    return output_payload(trace_step)[0]


def known_good_values(failing: m.StepDetail, healthy: m.StepDetail) -> list[m.KnownGoodValue]:
    bad = dict((p, v) for p, _, v in leaves(_semantic_output(failing), "/output"))
    good = dict((p, v) for p, _, v in leaves(_semantic_output(healthy), "/output"))
    values = []
    for pointer in [*good, *(p for p in bad if p not in good)]:
        if good.get(pointer) == bad.get(pointer):
            continue
        values.append(
            m.KnownGoodValue(
                citation=m.Citation(
                    addr=failing.addr, json_pointer=document_pointer(failing, pointer)
                ),
                value=good.get(pointer),
                failing_value=bad.get(pointer),
            )
        )
        if len(values) >= MAX_KNOWN_GOOD:
            break
    return values


def _edit_kind(step: m.StepDetail) -> m.EditKind | None:
    if not step.has_call:
        return None  # a pure state step makes no call, so there is nothing to replace
    return "override_output" if step.kind == "llm" else "patch_tool_result"


def _schema_fix(step: m.StepDetail) -> m.ProposedFix | None:
    """A rule-derived fix: stale data from a tool that accepts ``fresh`` is re-fetched."""
    stale = next((v for v in step.rule_violations if v.rule == "stale_as_of"), None)
    args = step.input.get("args") if isinstance(step.input, dict) else None
    if stale is None or not isinstance(args, dict) or args.get("fresh") is not False:
        return None
    age = (stale.evidence or {}).get("age_days")
    return m.ProposedFix(
        addr=step.addr,
        edit=m.ForkEdit(addr=step.addr, kind="patch_tool_args", value={"fresh": True}),
        source="schema_rule",
        rationale=(
            f"The tool returned data {age} days older than the rest of the run; "
            "re-fetch it with fresh=true."
            if age
            else "The tool returned stale data; re-fetch it with fresh=true."
        ),
        confidence=None,
    )


def propose_fixes(
    suspects: list[m.Suspect],
    steps: dict[str, m.StepDetail],
    twin: Twin | None,
    oracle: dict[str, Any],
) -> list[m.ProposedFix]:
    fixes: list[m.ProposedFix] = []
    for suspect in suspects:
        step = steps[suspect.addr]
        kind = _edit_kind(step)
        candidates: list[m.ProposedFix | None] = [_schema_fix(step)]
        twin_step = twin.steps.get(step.addr) if twin else None
        twin_fix = None
        if kind and twin_step is not None and twin_step.hashes.output != step.hashes.output:
            twin_fix = m.ProposedFix(
                addr=step.addr,
                edit=m.ForkEdit(
                    addr=step.addr, kind=kind, value=twin_step.output, known_good=twin.same_task
                ),
                source="nearest_twin",
                rationale=(
                    f"Use the output this step produced in passing run {twin.summary.run_id}"
                    + (" of the same task." if twin.same_task else " of a similar task.")
                ),
                confidence=round(suspect.probability, 4),
            )
        oracle_fix = None
        if kind and step.addr in oracle:
            oracle_fix = m.ProposedFix(
                addr=step.addr,
                edit=m.ForkEdit(
                    addr=step.addr, kind=kind, value=oracle[step.addr], known_good=True
                ),
                source="oracle",
                rationale="Replace the output with the known-good value from the task's oracle.",
                confidence=round(suspect.probability, 4),
            )
        if twin_fix is not None and twin and twin.same_task:
            candidates += [twin_fix, oracle_fix]
        else:
            candidates += [oracle_fix, twin_fix]
        best = next((fix for fix in candidates if fix is not None), None)
        if best is not None:
            fixes.append(best)
    return fixes


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _fault_label(name: str | None) -> str:
    return (name or "unknown").replace("_", " ")


def precedents_model(
    raw: dict[str, Any] | None, label_source: Callable[[str], str]
) -> m.Precedents | None:
    if not raw or not raw.get("neighbours"):
        return None
    fault = raw.get("dominant_fault_type")
    spec = FAULT_SPECS.get(fault)
    return m.Precedents(
        fault_type=fault,
        neighbours=raw["k"],
        same_fault=raw["dominant_count"],
        fix_summary=None,
        fix_tried=0,
        fix_worked=0,
        summary=(
            f"Looks like {raw['dominant_count']} of {raw['k']} past "
            f"{_fault_label(fault)} failures" + (f" ({spec.code}, {spec.family})." if spec else ".")
        ),
        cases=[
            m.PrecedentCase(
                run_id=case["run_id"],
                root_addr=case["addr"],
                fault_type=case.get("fault_type"),
                similarity=min(1.0, max(0.0, float(case["similarity"]))),
                fix_worked=None,
                label_source=label_source(case["run_id"]),  # type: ignore[arg-type]
            )
            for case in raw["neighbours"]
        ],
    )


def stage_rail(
    abstain: bool,
    reasons: list[m.Reason],
    fixes: list[m.ProposedFix],
    verification: m.Verification | None,
) -> m.StageRail:
    states: dict[str, m.StageState] = {
        "localize": "abstained" if abstain else "done",
        "attribute": "done" if reasons else "pending",
        "propose": "done" if fixes else "blocked",
        "verify": "pending",
    }
    if verification is not None:
        if verification.status == "complete":
            states["verify"] = "done"
        elif verification.status == "running":
            states["verify"] = "active"
    elif not fixes:
        states["verify"] = "blocked"
    order: list[m.Stage] = ["localize", "attribute", "propose", "verify"]
    current = next((s for s in order if states[s] in {"pending", "active"}), "verify")
    return m.StageRail(**states, current=current)


def narrative(
    run: m.RunSummary,
    steps: dict[str, m.StepDetail],
    responsible: str | None,
    probability: float | None,
    visible: str | None,
    path: m.DamagePath | None,
    reasons: list[m.Reason],
) -> m.Narrative:
    claims: list[m.NarrativeClaim] = []
    if visible:
        reason = f" ({run.checker_reason})" if run.checker_reason else ""
        claims.append(
            m.NarrativeClaim(
                text=f"The failure became visible at {steps[visible].name}{reason}.",
                cites=[m.Citation(addr=visible, json_pointer=None)],
            )
        )
    if responsible and probability is not None:
        claims.append(
            m.NarrativeClaim(
                text=(
                    f"The most likely responsible step is {steps[responsible].name} "
                    f"({probability:.0%})."
                ),
                cites=[m.Citation(addr=responsible, json_pointer=None)],
            )
        )
        if path is not None:
            symptoms = [node for node in path.nodes if node.tag == "symptom"]
            reach = " and reaches the final answer" if path.reaches_final_answer else ""
            claims.append(
                m.NarrativeClaim(
                    text=f"Its output flows into {len(symptoms)} later steps{reach}.",
                    cites=[m.Citation(addr=responsible, json_pointer=None)],
                )
            )
    for reason in reasons:
        if reason.suspect_addr == responsible:
            claims.append(m.NarrativeClaim(text=reason.text, cites=[reason.citation]))
    if responsible:
        summary = f"{run.task}: failed. Probable cause: {steps[responsible].name}" + (
            f" ({probability:.0%})." if probability is not None else "."
        )
    else:
        summary = f"{run.task}: failed. No single step could be blamed with confidence."
    return m.Narrative(summary=summary, claims=claims, source="template", validated=True)


def build_diagnosis(
    *,
    diagnoser: Diagnoser,
    report: DiagnosisReport,
    detail: m.RunDetail,
    latency_ms: float,
    twin: Twin | None,
    oracle: dict[str, Any],
    verification: m.Verification | None,
    label_source: Callable[[str], str],
) -> m.Diagnosis:
    steps = {step.addr: step for step in detail.steps}
    probabilities = dict(report.ranking)
    ordered = sorted(
        steps.values(),
        key=lambda step: (-probabilities.get(step.addr, 0.0), step.seq),
    )
    if not probabilities:
        raise ValueError("the diagnoser produced no ranking")
    conformal = [addr for addr in report.conformal_set if addr in steps]
    visible = report.visible_failure if report.visible_failure in steps else None
    path = None
    abstain = report.abstain
    responsible = None if abstain else report.ranking[0][0]
    root_for_path = responsible or report.ranking[0][0]
    if root_for_path in steps:
        path = damage_path(detail.steps, detail.edges, root_for_path, visible)
    on_path = {
        node.addr for node in (path.nodes if path else []) if node.tag in {"root", "symptom"}
    }

    suspects = []
    for rank, step in enumerate(ordered, start=1):
        flags: list[m.SuspectFlag] = []
        if step.addr == visible:
            flags.append("visible_failure")
        if step.error is not None and step.error.recovered:
            flags.append("error_recovered")
        if step.rule_violations:
            flags.append("schema_violation")
        if step.addr == report.first_anomaly:
            flags.append("first_anomaly")
        if step.addr in on_path and not is_evaluation_step(step.addr, step.agent_role):
            flags.append("on_damage_path")
        probability = probabilities.get(step.addr, 0.0)
        suspects.append(
            m.Suspect(
                rank=rank,
                addr=step.addr,
                name=step.name,
                kind=step.kind,
                score=round(report.scores.get(step.addr, 0.0), 6),
                probability=round(probability, 6),
                in_conformal_set=step.addr in conformal,
                flags=flags,
            )
        )
    _normalise(suspects)

    reasons: list[m.Reason] = []
    for suspect in report.suspects[:TOP_REASONED]:
        for reason in suspect.reasons:
            cited = reason.citations[0] if reason.citations else None
            reasons.append(
                m.Reason(
                    suspect_addr=suspect.addr,
                    text=reason.text,
                    citation=citation(steps, cited.addr, cited.pointer)
                    if cited
                    else m.Citation(addr=suspect.addr, json_pointer=None),
                    feature=m.FeatureAttribution(
                        name=reason.feature,
                        group=GROUP_NAMES.get(reason.group, reason.group).title(),
                        value=None if math.isnan(reason.value) else round(reason.value, 4),
                        contribution=round(reason.contribution, 4),
                    ),
                )
            )

    evidence: list[m.RuleViolation] = []
    seen: set[tuple[str, str | None, str]] = set()

    def add(violation: m.RuleViolation) -> None:
        key = (violation.citation.addr, violation.citation.json_pointer, violation.message)
        if violation.citation.addr in steps and key not in seen:
            seen.add(key)
            evidence.append(violation)

    for step in detail.steps:
        for violation in step.rule_violations:
            add(violation)
    for suspect in report.suspects[:TOP_REASONED]:
        for finding in report.findings_by_step.get(suspect.addr, [])[:4]:
            add(rule_from_finding(steps, finding))

    top_suspects = [s for s in suspects if not is_evaluation_step(s.addr, steps[s.addr].agent_role)]
    fixes = propose_fixes(top_suspects[:TOP_REASONED], steps, twin, oracle)
    nearest = None
    if twin is not None:
        root = steps.get(root_for_path)
        twin_root = twin.steps.get(root_for_path)
        nearest = m.NearestTwin(
            run=twin.summary,
            similarity=round(min(1.0, max(0.0, twin.similarity)), 4),
            same_task=twin.same_task,
            basis=twin.basis,  # type: ignore[arg-type]
            known_good_values=known_good_values(root, twin_root) if root and twin_root else [],
        )
    top_probability = probabilities.get(responsible) if responsible else None
    max_set = diagnoser.config.max_set
    return m.Diagnosis(
        run_id=detail.run.run_id,
        model_version=report.model_version,
        computed_at=datetime.now(UTC),
        latency_ms=round(latency_ms, 3),
        fixture=False,
        ranking=suspects,
        conformal_set=conformal,
        coverage_target=round(1 - diagnoser.config.alpha, 4),
        abstain=abstain,
        abstain_reason="set_too_large" if abstain else None,
        abstain_message=(
            f"No confident culprit: reaching {round((1 - diagnoser.config.alpha) * 100)}% "
            f"coverage needs {len(conformal)} candidate steps (more than {max_set})."
            if abstain
            else None
        ),
        visible_failure_addr=visible,
        responsible_addr=responsible,
        reasons=reasons,
        rule_evidence=evidence,
        damage_path=path,
        precedents=precedents_model(report.precedents, label_source),
        nearest_twin=nearest,
        proposed_fixes=fixes,
        verification=verification,
        stages=stage_rail(abstain, reasons, fixes, verification),
        narrative=narrative(
            detail.run, steps, responsible, top_probability, visible, path, reasons
        ),
    )


def _normalise(suspects: list[m.Suspect]) -> None:
    """Rounded probabilities must still sum to 1; put the residue on the top step."""
    total = sum(s.probability for s in suspects)
    if suspects and abs(total - 1.0) > 1e-9:
        suspects[0].probability = round(
            min(1.0, max(0.0, suspects[0].probability + 1.0 - total)), 6
        )
