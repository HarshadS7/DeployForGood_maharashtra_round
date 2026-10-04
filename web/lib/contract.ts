/* eslint-disable */
// GENERATED FILE. Source of truth: server/models.py.
// Regenerate with `make contract` (python -m server.contract_export).

export const CONTRACT_VERSION = "1.0.0";

export interface AblationArm {
  arm: string;
  label: string;
  status: "ran" | "not_run";
  reason: string | null;
  top1: Estimate | null;
  /**
   * Arm minus reference on the same runs; null for the reference itself.
   */
  diff_vs_reference: PairedDifference | null;
}
/**
 * A point estimate with an optional 95% interval (bootstrap over runs unless noted).
 */
export interface Estimate {
  value: number;
  ci_low: number | null;
  ci_high: number | null;
}
/**
 * Paired bootstrap difference (arm minus reference) over the same runs.
 */
export interface PairedDifference {
  delta: number;
  ci_low: number;
  ci_high: number;
  n: number;
  n_boot: number;
  excludes_zero: boolean;
}
export interface AbstentionPoint {
  answered_fraction: number;
  top1_when_answered: number | null;
  n: number;
}
export interface AgentInfo {
  agent_id: string;
  display_name: string;
  description: string;
  client: "llm" | "deterministic_standin" | "imported";
  models: string[];
  runs: number;
  passed: number;
  failed: number;
  typical_steps: number | null;
}
export interface AgentList {
  items: AgentInfo[];
}
/**
 * GET /health. Cheap enough for a platform health check.
 */
export interface AppHealth {
  status: "ok" | "degraded";
  api_version: string;
  mode: "live" | "recorded" | "offline";
  /**
   * True when served from web/public/recorded.
   */
  static_bundle: boolean;
  /**
   * Diagnoser artifact version, null if absent.
   */
  model_version: string | null;
  dataset_version: string | null;
  capabilities: Capabilities;
  counts: HealthCounts;
  started_at: string;
  notes: string[];
}
/**
 * What this deployment can do. The UI disables controls instead of letting them fail.
 */
export interface Capabilities {
  diagnose: boolean;
  /**
   * POST /forks accepts edits (false for the static bundle).
   */
  fork: boolean;
  /**
   * Replay may call a real model or tool.
   */
  live_calls: boolean;
  verify: boolean;
  export_test: boolean;
  label: boolean;
  ingest_otlp: boolean;
  eval: boolean;
}
export interface HealthCounts {
  runs: number;
  steps: number;
  forks: number;
  labels: number;
}
export interface Calibration {
  split: "S0" | "S1" | "S3" | "S4" | "S5";
  status: "ran" | "not_run";
  reason: string | null;
  bins: ReliabilityBin[];
  abstention: AbstentionPoint[];
  ece: number | null;
  conformal_target: number | null;
  conformal_empirical: number | null;
  mean_set_size: number | null;
  abstain_rate: number | null;
}
export interface ReliabilityBin {
  lo: number;
  hi: number;
  n: number;
  mean_confidence: number | null;
  accuracy: number | null;
}
/**
 * A pointer to evidence. `json_pointer` is relative to the step document.
 *
 * The step document is `{input, output, reasoning, state_before, state_after}` of
 * `StepDetail`. Edges recorded by the SDK use the shorthand `/state/<key>`, which means
 * `state_after` on the producing side and `state_before` on the consuming side.
 */
export interface Citation {
  addr: string;
  json_pointer: string | null;
}
/**
 * An edge on the forward path from the clicked value; `depth` 1 is a direct consumer.
 */
export interface ConsumerHop {
  src_addr: string;
  src_pointer: string | null;
  dst_addr: string;
  dst_pointer: string | null;
  kind: "state" | "message" | "inferred";
  key: string | null;
  version: number | null;
  value_hash: string | null;
  depth: number;
}
/**
 * Token and money cost of one run. `usd` is null when the model has no known price.
 */
export interface Cost {
  llm_calls: number;
  tokens_in: number;
  tokens_out: number;
  tokens_cached: number;
  usd: number | null;
}
/**
 * Structured crash report; the Markdown export is rendered from the same content.
 */
export interface CrashReport {
  run_id: string;
  title: string;
  generated_at: string;
  synopsis: string;
  sequence_of_events: ReportEvent[];
  probable_cause: ReportClaim | null;
  contributing_factors: ReportClaim[];
  findings: ReportClaim[];
  recommended_fix: ReportClaim | null;
  verification: ReportClaim | null;
  fixture: boolean;
}
export interface ReportEvent {
  seq: number;
  addr: string;
  text: string;
  tag: ("root" | "symptom" | "unaffected") | null;
}
export interface ReportClaim {
  text: string;
  cites: Citation[];
}
export interface DamageNode {
  addr: string;
  name: string;
  kind: "llm" | "tool" | "retrieval" | "state" | "value";
  tag: "root" | "symptom" | "unaffected";
  is_visible_failure: boolean;
}
/**
 * The suspect's forward cone. Every step appears once, tagged root/symptom/unaffected.
 */
export interface DamagePath {
  root_addr: string;
  nodes: DamageNode[];
  edges: Edge[];
  reaches_final_answer: boolean;
}
/**
 * A dependency between two steps. Pointers are relative to each step's document.
 */
export interface Edge {
  src_addr: string;
  src_pointer: string | null;
  dst_addr: string;
  dst_pointer: string | null;
  kind: "state" | "message" | "inferred";
  key: string | null;
  version: number | null;
  value_hash: string | null;
}
export interface DatasetCounts {
  base_runs: number;
  injected_forks: number;
  accepted_failures: number;
  natural_failures: number;
  human_labelled: number;
  runs_per_split: {
    [k: string]: number;
  };
  failures_per_family: {
    [k: string]: number;
  };
}
export interface DatasetInfo {
  /**
   * Hash of the frozen dataset (data/DATASET_VERSION).
   */
  version: string;
  frozen_at: string | null;
  agents: string[];
  held_out_fault_codes: string[];
  counts: DatasetCounts;
}
/**
 * Run-level failure detector: powers the risk column in the Runs table.
 */
export interface DetectorMetrics {
  status: "ran" | "not_run";
  reason: string | null;
  split: ("S0" | "S1" | "S3" | "S4" | "S5") | null;
  n_runs: number;
  n_failed: number;
  auroc: Estimate | null;
  accuracy: Estimate | null;
}
/**
 * GET /runs/{id}/diagnosis: ranked suspects, evidence, proof, and an honest abstain path.
 */
export interface Diagnosis {
  run_id: string;
  model_version: string;
  computed_at: string;
  latency_ms: number;
  /**
   * True for hand-authored illustrative content.
   */
  fixture: boolean;
  /**
   * Every step, most suspect first.
   *
   * @minItems 1
   */
  ranking: [Suspect, ...Suspect[]];
  conformal_set: string[];
  coverage_target: number;
  abstain: boolean;
  abstain_reason: ("margin_too_small" | "set_too_large" | "no_signal" | "model_unavailable") | null;
  /**
   * Headline shown instead of a culprit.
   */
  abstain_message: string | null;
  /**
   * Where the failure showed up. Not necessarily the responsible step.
   */
  visible_failure_addr: string | null;
  /**
   * Top suspect; null when abstaining.
   */
  responsible_addr: string | null;
  reasons: Reason[];
  rule_evidence: RuleViolation[];
  damage_path: DamagePath | null;
  precedents: Precedents | null;
  nearest_twin: NearestTwin | null;
  proposed_fixes: ProposedFix[];
  verification: Verification | null;
  stages: StageRail;
  narrative: Narrative | null;
}
export interface Suspect {
  rank: number;
  addr: string;
  name: string;
  kind: "llm" | "tool" | "retrieval" | "state" | "value";
  score: number;
  /**
   * Softmax over the run's step scores, temperature fitted.
   */
  probability: number;
  in_conformal_set: boolean;
  flags: (
    "visible_failure" | "error_recovered" | "schema_violation" | "first_anomaly" | "on_damage_path"
  )[];
}
/**
 * One plain-language reason the model scored the suspect high.
 */
export interface Reason {
  suspect_addr: string;
  text: string;
  citation: Citation;
  feature: FeatureAttribution;
}
export interface FeatureAttribution {
  name: string;
  /**
   * Feature group, e.g. 'Lineage' or 'Validity'.
   */
  group: string;
  value: number | string | boolean | null;
  /**
   * Signed contribution to the suspect's score.
   */
  contribution: number;
}
/**
 * A deterministic schema or protocol rule that failed. Evidence, not model prose.
 */
export interface RuleViolation {
  rule:
    | "empty_query"
    | "empty_result"
    | "swallowed_error"
    | "repeated_call"
    | "schema_invalid"
    | "non_finite_or_out_of_range"
    | "unit_token_mismatch"
    | "stale_as_of"
    | "error_status_in_output"
    | "other";
  severity: "error" | "warning" | "info";
  message: string;
  citation: Citation;
  evidence: {
    [k: string]: unknown;
  } | null;
}
/**
 * 'Looks like 9 of 12 past stale-data failures; fresh=true fixed 7 of them.'
 */
export interface Precedents {
  fault_type: string | null;
  neighbours: number;
  same_fault: number;
  fix_summary: string | null;
  fix_tried: number;
  fix_worked: number;
  summary: string;
  cases: PrecedentCase[];
}
export interface PrecedentCase {
  run_id: string;
  root_addr: string;
  fault_type: string | null;
  similarity: number;
  fix_worked: boolean | null;
  label_source: "injected" | "natural_auto" | "human" | "verified";
}
/**
 * The nearest passing run of the same task or agent: a source of known-good values.
 */
export interface NearestTwin {
  run: RunSummary;
  similarity: number;
  same_task: boolean;
  basis: ("agent" | "task_embedding" | "graph_shape")[];
  known_good_values: KnownGoodValue[];
}
/**
 * One row of the Runs table.
 */
export interface RunSummary {
  run_id: string;
  status: "passed" | "failed" | "running";
  agent: string;
  task_id: string;
  /**
   * One-line task summary for the table.
   */
  task: string;
  origin: "natural" | "injected" | "fork" | "imported";
  parent_run_id: string | null;
  fork_id: string | null;
  mode: "live" | "recorded" | "offline" | "replay";
  model: string | null;
  /**
   * 'deterministic_standin' runs come from an offline test double, not a model.
   */
  client: "llm" | "deterministic_standin" | "imported";
  seed: number | null;
  steps: number;
  duration_ms: number;
  cost: Cost;
  started_at: string;
  ended_at: string | null;
  score: number | null;
  checker_reason: string | null;
  split: ("S0" | "S1" | "S3" | "S4" | "S5") | null;
  /**
   * Run-level failure probability from the detector.
   */
  risk: number | null;
  top_suspect: SuspectBrief | null;
  /**
   * Groups runs that fail alike: '<agent>:<suspect step name>:<reason class>'.
   */
  failure_signature: string | null;
  label: LabelInfo | null;
}
export interface SuspectBrief {
  addr: string;
  name: string;
  probability: number;
}
/**
 * Ground truth for a run, when known. Hidden (null) in blind/Label mode.
 */
export interface LabelInfo {
  source: "injected" | "natural_auto" | "human" | "verified";
  root_addr: string;
  /**
   * Fault Forge operator code such as 'T2'.
   */
  fault_code: string | null;
  fault_type: string | null;
  fault_family: ("tool" | "retrieval" | "decision" | "coordination") | null;
  /**
   * True for fault types never seen in training.
   */
  held_out: boolean | null;
  /**
   * The agent recovered, so the run still passed.
   */
  recovered: boolean;
  /**
   * First step whose behaviour changed.
   */
  manifest_addr: string | null;
  verified: boolean;
  confidence: ("high" | "low") | null;
}
export interface KnownGoodValue {
  citation: Citation;
  value: unknown;
  failing_value: unknown;
}
/**
 * The *Propose* stage: a fix the Fork drawer is pre-filled with.
 */
export interface ProposedFix {
  addr: string;
  edit: ForkEdit;
  source: "schema_rule" | "oracle" | "nearest_twin" | "precedent" | "llm" | "manual";
  rationale: string;
  confidence: number | null;
}
/**
 * One change applied to one step of the base run.
 *
 * `value` by kind: `override_output` and `patch_tool_result` take the replacement
 * payload; `patch_tool_args` an object merged into the tool arguments; `patch_prompt` a
 * string appended to the system prompt or an object merged into the chat request;
 * `swap_model` the model id.
 */
export interface ForkEdit {
  addr: string;
  kind: "override_output" | "patch_tool_args" | "patch_prompt" | "swap_model" | "patch_tool_result";
  value: unknown;
  /**
   * The value is an oracle or nearest-passing-twin value, which enables REFUTED.
   */
  known_good?: boolean;
}
/**
 * What the intervention says, kept separate from what the model suspects.
 */
export interface Verification {
  verdict: ("VERIFIED" | "REFUTED" | "INCONCLUSIVE") | null;
  status: "not_started" | "running" | "complete";
  fork_id: string | null;
  edit_addr: string | null;
  k: number | null;
  fix: PassRate | null;
  control: PassRate | null;
  replay_fidelity: number | null;
  preview: boolean;
  /**
   * Why this verdict, or what would resolve it.
   */
  explanation: string | null;
}
/**
 * k of n passes with the Wilson 95% interval. The interval is checked, not trusted.
 */
export interface PassRate {
  passed: number;
  total: number;
  rate: number;
  ci_low: number;
  ci_high: number;
}
export interface StageRail {
  localize: "pending" | "active" | "done" | "abstained" | "blocked";
  attribute: "pending" | "active" | "done" | "abstained" | "blocked";
  propose: "pending" | "active" | "done" | "abstained" | "blocked";
  verify: "pending" | "active" | "done" | "abstained" | "blocked";
  current: "localize" | "attribute" | "propose" | "verify";
}
/**
 * Optional incident-style summary. Every claim cites an address that exists.
 */
export interface Narrative {
  summary: string;
  claims: NarrativeClaim[];
  source: "template" | "llm";
  validated: boolean;
}
export interface NarrativeClaim {
  text: string;
  /**
   * @minItems 1
   */
  cites: [Citation, ...Citation[]];
}
/**
 * GET /diff?a=&b=. Rows are aligned by address, or by LCS when control flow changed.
 */
export interface DiffResponse {
  left_run_id: string;
  right_run_id: string;
  alignment: "address" | "lcs";
  first_divergence: string | null;
  stats: DiffStats;
  rows: DiffRow[];
  state_diff: StateKeyDiff[];
  outcome: OutcomeDiff;
  nearest_passing: NearestPassingLink | null;
}
export interface DiffStats {
  same: number;
  cached: number;
  changed: number;
  new: number;
  removed: number;
}
export interface DiffRow {
  addr: string;
  status: "same" | "cached" | "changed" | "new" | "removed";
  in_cone: boolean;
  changed: ("input" | "output" | "state")[];
  left: DiffSide | null;
  right: DiffSide | null;
}
/**
 * One side of an aligned row. Payloads are omitted for same/cached rows.
 */
export interface DiffSide {
  seq: number;
  kind: "llm" | "tool" | "retrieval" | "state" | "value";
  name: string;
  cache_status: "cached" | "invalidated" | "live" | "edited";
  input: unknown;
  output: unknown;
  state_writes: {
    [k: string]: unknown;
  };
  input_hash: string | null;
  output_hash: string | null;
  state_after_hash: string | null;
}
export interface StateKeyDiff {
  key: string;
  status: "unchanged" | "changed" | "added" | "removed";
  left: unknown;
  right: unknown;
  left_hash: string | null;
  right_hash: string | null;
}
export interface OutcomeDiff {
  left: OutcomeBrief;
  right: OutcomeBrief;
  flipped: boolean;
  direction: "fail_to_pass" | "pass_to_fail" | "unchanged";
}
export interface OutcomeBrief {
  status: "passed" | "failed" | "running";
  score: number | null;
  reason: string | null;
}
export interface NearestPassingLink {
  run_id: string;
  similarity: number;
  reason: string;
}
/**
 * Machine-readable error. `code` is stable; `message` is for people.
 */
export interface ErrorBody {
  code:
    | "bad_request"
    | "validation_error"
    | "not_found"
    | "conflict"
    | "live_call_refused"
    | "replay_divergence"
    | "not_verified"
    | "not_applicable"
    | "rate_limited"
    | "unavailable"
    | "unsupported"
    | "internal_error";
  message: string;
  status: number;
  /**
   * What the caller can do next, when there is a next step.
   */
  hint: string | null;
  issues: ValidationIssue[];
  context: {
    [k: string]: unknown;
  } | null;
  request_id: string | null;
}
/**
 * One request-validation problem (the shape of a FastAPI 422 entry, normalised).
 */
export interface ValidationIssue {
  loc: (string | number)[];
  message: string;
  type: string;
}
/**
 * Uniform body of every non-2xx response, including the SSE `error` event's data.
 */
export interface ErrorResponse {
  error: ErrorBody;
}
/**
 * GET /eval: every table the Results page draws. Numbers always carry n and an interval.
 */
export interface EvalResponse {
  fixture: boolean;
  /**
   * Shown prominently when fixture is true; null otherwise.
   */
  fixture_notice: string | null;
  generated_at: string;
  dataset: DatasetInfo;
  model: ModelInfo;
  headline: HeadlineCard[];
  leaderboard: LeaderboardRow[];
  generalisation: GeneralisationMatrix;
  feature_group_ablation: FeatureGroupAblation;
  recorder_ablations: PairedAblation[];
  integrity: IntegrityCheck[];
  calibration: Calibration;
  replay_savings: ReplaySavings;
  detector: DetectorMetrics;
  not_run: NotRunItem[];
  notes: string[];
}
export interface ModelInfo {
  model_version: string;
  backend: "lightgbm" | "sklearn-hgb";
  trained_at: string | null;
  n_features: number;
  feature_groups: FeatureGroupInfo[];
  temperature: number | null;
  conformal_coverage: number | null;
  trained_on_runs: number;
}
export interface FeatureGroupInfo {
  id: string;
  name: string;
  n_features: number;
  enabled: boolean;
}
export interface HeadlineCard {
  id: string;
  title: string;
  status: "ran" | "not_run";
  reason: string | null;
  value: Estimate | null;
  unit: "fraction" | "ms" | "count";
  n: number | null;
  comparator_name: string | null;
  comparator: Estimate | null;
  tooltip: string;
}
/**
 * One method on one split. Not-run and literature rows are explicit, never blank.
 */
export interface LeaderboardRow {
  method: string;
  method_kind: "ranker" | "baseline" | "llm_judge" | "literature";
  split: "S0" | "S1" | "S3" | "S4" | "S5";
  status: "ran" | "not_run";
  reason: string | null;
  /**
   * A published number that this repository did not reproduce.
   */
  reported_literature: boolean;
  source: string | null;
  n: number | null;
  top1: Estimate | null;
  top3: Estimate | null;
  within1: Estimate | null;
  mrr: Estimate | null;
  latency_ms: number | null;
}
export interface GeneralisationMatrix {
  metric: "top1";
  methods: string[];
  splits: ("S0" | "S1" | "S3" | "S4" | "S5")[];
  cells: MatrixCell[];
}
export interface MatrixCell {
  method: string;
  split: "S0" | "S1" | "S3" | "S4" | "S5";
  value: number | null;
  n: number | null;
  status: "ran" | "not_run";
  reason: string | null;
}
export interface FeatureGroupAblation {
  split: "S0" | "S1" | "S3" | "S4" | "S5";
  n: number | null;
  full: Estimate | null;
  rows: FeatureGroupAblationRow[];
}
export interface FeatureGroupAblationRow {
  group_id: string;
  group_name: string;
  status: "ran" | "not_run";
  reason: string | null;
  top1_without: Estimate | null;
  delta_vs_full: PairedDifference | null;
}
/**
 * A recorder-value ablation. Reported even when the full record does not win.
 */
export interface PairedAblation {
  id: "provenance_edges" | "telemetry_sufficiency";
  title: string;
  split: "S0" | "S1" | "S3" | "S4" | "S5";
  n: number | null;
  reference_arm: string;
  arms: AblationArm[];
  interpretation: ("reference_wins" | "no_significant_difference" | "reference_loses") | null;
}
export interface IntegrityCheck {
  id: "artifact_audit" | "shuffled_labels" | "human_agreement";
  title: string;
  status: "ran" | "not_run";
  reason: string | null;
  value: Estimate | null;
  threshold: number | null;
  passes: boolean | null;
  n: number | null;
  detail: string;
}
export interface ReplaySavings {
  status: "ran" | "not_run";
  reason: string | null;
  n_forks: number;
  by_mode: ModeSavings[];
  calls_saved_fraction: Estimate | null;
  tokens_saved: number;
  ms_saved: number;
  verifier: VerifierStats | null;
}
export interface ModeSavings {
  mode: "cone" | "prefix" | "full";
  mean_steps_reexecuted: number;
  mean_fraction_reexecuted: number;
  n: number;
}
export interface VerifierStats {
  n_known_roots: number;
  verified: number;
  refuted: number;
  inconclusive: number;
  n_wrong_roots: number;
  falsely_verified: number;
}
export interface NotRunItem {
  section: string;
  item: string;
  reason: string;
}
export interface ExportTestRequest {
  overwrite?: boolean;
}
/**
 * POST /forks/{id}/export-test. Only a VERIFIED fork can be exported.
 */
export interface ExportTestResponse {
  export_id: string;
  fork_id: string;
  verdict: "VERIFIED";
  test_path: string;
  fixture_dir: string;
  test_sha256: string;
  fixture_sha256: string;
  files: ExportedFile[];
  invalidation_cone: string[];
  run_command: string;
  created_at: string;
}
export interface ExportedFile {
  path: string;
  sha256: string;
  bytes: number;
}
/**
 * '14 runs fail like this': failed runs sharing one failure_signature.
 */
export interface FailureGroup {
  signature: string;
  label: string;
  agent: string;
  count: number;
  top_suspect_name: string | null;
  fault_type: string | null;
  example_run_ids: string[];
}
export interface FailureGroupList {
  total_failed: number;
  items: FailureGroup[];
  fixture: boolean;
}
/**
 * 202 response of POST /forks.
 */
export interface ForkCreated {
  fork_id: string;
  base_run_id: string;
  branch_name: string;
  status: "queued" | "running" | "complete" | "error";
  stream_url: string;
  prediction: ReplayPrediction | null;
  created_at: string;
}
/**
 * POST /replay/predict: what a fork will cost, before running it.
 *
 * `expected` is the static dependency cone: edited steps are `edited`, their transitive
 * consumers `invalidated`, everything else `cached`. Early cutoff can only shrink it.
 */
export interface ReplayPrediction {
  base_run_id: string;
  mode: "cone" | "prefix" | "full";
  samples: number;
  total_steps: number;
  reexecute_steps: number;
  /**
   * LLM/tool/value calls in the base run.
   */
  total_calls: number;
  reexecute_calls: number;
  per_step: StepPrediction[];
  estimated_ms: number | null;
  estimated_tokens: number | null;
  upper_bound: boolean;
  /**
   * 'Will re-run 4 of 13 calls (6 of 16 steps), about 12 ms.'
   */
  summary: string;
  /**
   * Why this fork would be refused in the current mode, or null.
   */
  blocked: ErrorBody | null;
}
export interface StepPrediction {
  addr: string;
  expected: "cached" | "invalidated" | "live" | "edited";
  has_call: boolean;
}
/**
 * POST /forks and POST /replay/predict.
 *
 * `edits` non-empty: *Fork with edit*. `edits` empty: *Re-run unchanged*, which resamples
 * the cone of `resample_from` with no edit and is the paired control on its own.
 */
export interface ForkRequest {
  base_run_id: string;
  /**
   * @maxItems 8
   */
  edits?:
    | []
    | [ForkEdit]
    | [ForkEdit, ForkEdit]
    | [ForkEdit, ForkEdit, ForkEdit]
    | [ForkEdit, ForkEdit, ForkEdit, ForkEdit]
    | [ForkEdit, ForkEdit, ForkEdit, ForkEdit, ForkEdit]
    | [ForkEdit, ForkEdit, ForkEdit, ForkEdit, ForkEdit, ForkEdit]
    | [ForkEdit, ForkEdit, ForkEdit, ForkEdit, ForkEdit, ForkEdit, ForkEdit]
    | [ForkEdit, ForkEdit, ForkEdit, ForkEdit, ForkEdit, ForkEdit, ForkEdit, ForkEdit];
  /**
   * Required when edits is empty; forbidden otherwise.
   */
  resample_from?: string | null;
  mode?: "cone" | "prefix" | "full";
  samples?: number;
  /**
   * Run K unchanged samples with identical seeds. Without it the result is a preview and is never VERIFIED.
   */
  control?: boolean;
  hypothesis?: string | null;
  branch_name?: string | null;
}
export interface ForkRunRef {
  run_id: string;
  sample: number;
  branch: "fix" | "control";
  passed: boolean;
  score: number | null;
  reason: string | null;
}
/**
 * A recorded SSE transcript, used by the UI as a mock stream. Not an API response.
 */
export interface ForkStreamRecording {
  fork_id: string;
  description: string;
  provenance: "engine_recording" | "engine_recording_noisy_standin";
  events: (StepEvent | OutcomeEvent | SummaryEvent | StreamErrorEvent)[];
}
export interface StepEvent {
  id: number;
  event: "step";
  data: StepEventData;
}
export interface StepEventData {
  addr: string;
  phase: "queued" | "running" | "done" | "diverged";
  /**
   * Null until the status is known (e.g. a queued step that is not invalidated).
   */
  cache_status: ("cached" | "invalidated" | "live" | "edited") | null;
  ms: number | null;
  tokens: number | null;
  sample: number;
  branch: "fix" | "control";
}
export interface OutcomeEvent {
  id: number;
  event: "outcome";
  data: OutcomeEventData;
}
export interface OutcomeEventData {
  run_id: string | null;
  sample: number;
  branch: "fix" | "control";
  passed: boolean;
  reason: string | null;
  is_control: boolean;
}
export interface SummaryEvent {
  id: number;
  event: "summary";
  data: SummaryEventData;
}
/**
 * Final tally. Step counts are per edited run; tokens and time are summed over all runs.
 */
export interface SummaryEventData {
  fork_id: string;
  reexecuted: number;
  cached: number;
  invalidated: number;
  total_steps: number;
  calls_reexecuted: number;
  calls_cached: number;
  tokens_saved: number;
  ms_saved: number;
  fix_rate: number;
  /**
   * @minItems 2
   * @maxItems 2
   */
  fix_ci: [number, number];
  control_rate: number | null;
  control_ci: [number, number] | null;
  k: number;
  verdict: ("VERIFIED" | "REFUTED" | "INCONCLUSIVE") | null;
  /**
   * True when there is no paired control.
   */
  preview: boolean;
}
export interface StreamErrorEvent {
  id: number;
  event: "error";
  data: ErrorBody;
}
/**
 * GET /forks/{id}: an immutable named branch with its paired result.
 */
export interface ForkSummary {
  fork_id: string;
  base_run_id: string;
  branch_name: string;
  status: "queued" | "running" | "complete" | "error";
  hypothesis: string | null;
  edits: ForkEdit[];
  mode: "cone" | "prefix" | "full";
  samples: number;
  control: boolean;
  created_at: string;
  completed_at: string | null;
  savings: Savings | null;
  fix: PassRate | null;
  control_result: PassRate | null;
  verdict: ("VERIFIED" | "REFUTED" | "INCONCLUSIVE") | null;
  preview: boolean;
  /**
   * Share of pre-edit steps whose replayed state hash matched the recording.
   */
  replay_fidelity: number | null;
  edited_runs: ForkRunRef[];
  control_runs: ForkRunRef[];
  /**
   * True only for a VERIFIED fork.
   */
  export_available: boolean;
  error: ErrorBody | null;
}
export interface Savings {
  reexecuted: number;
  cached: number;
  invalidated: number;
  total_steps: number;
  calls_reexecuted: number;
  calls_cached: number;
  tokens_saved: number;
  ms_saved: number;
}
/**
 * GET /runs/{id}/forks.
 */
export interface ForkTimeline {
  base_run_id: string;
  entries: TimelineEntry[];
  forks: ForkSummary[];
}
/**
 * One node of the original -> control -> edited-fork timeline.
 */
export interface TimelineEntry {
  kind: "original" | "control" | "fork";
  label: string;
  run_id: string | null;
  fork_id: string | null;
  /**
   * For a control entry: the fork it pairs with.
   */
  parent_fork_id: string | null;
  outcome: ("passed" | "failed" | "running") | null;
  score: number | null;
  checker_reason: string | null;
  pass_rate: PassRate | null;
  verdict: ("VERIFIED" | "REFUTED" | "INCONCLUSIVE") | null;
  preview: boolean;
  savings: Savings | null;
  edit_summary: string | null;
}
export interface KappaStatus {
  kappa: number | null;
  /**
   * Runs labelled by at least two annotators.
   */
  n: number;
  annotators: number;
}
export interface LabelProgress {
  labelled: number;
  target: number;
  remaining: number;
}
export interface LabelQueue {
  progress: LabelProgress;
  items: LabelTask[];
}
/**
 * A failed run shown with the model's guess and the ground truth hidden.
 */
export interface LabelTask {
  run_id: string;
  agent: string;
  task: string;
  checker_reason: string | null;
  steps: number;
  labelled_by_you: boolean;
}
/**
 * POST /labels. `root_addr` null means the annotator found no responsible step.
 */
export interface LabelRequest {
  run_id: string;
  annotator: string;
  root_addr: string | null;
  certainty?: "sure" | "unsure";
  notes?: string | null;
}
export interface LabelResponse {
  label_id: string;
  run_id: string;
  annotator: string;
  root_addr: string | null;
  created_at: string;
  progress: LabelProgress;
  agreement: KappaStatus;
}
/**
 * The OTLP ExportTraceServiceResponse plus what Black Box built from the spans.
 */
export interface OtlpIngestAck {
  partialSuccess: OtlpPartialSuccess | null;
  accepted_spans: number;
  runs: OtlpRunRef[];
  /**
   * Always false: imported traces cannot be replayed.
   */
  replayable: boolean;
  note: string;
}
/**
 * OTLP/HTTP wire names (camelCase), so stock exporters accept the response.
 */
export interface OtlpPartialSuccess {
  rejectedSpans: number;
  errorMessage: string;
}
export interface OtlpRunRef {
  run_id: string;
  spans: number;
}
export interface ProducerCandidate {
  addr: string;
  name: string;
  json_pointer: string | null;
  edge_kind: "state" | "message" | "inferred";
  key: string | null;
  version: number | null;
  value_hash: string | null;
}
/**
 * GET /runs/{id}: the run, every step with payloads, and every provenance edge.
 */
export interface RunDetail {
  run: RunSummary;
  /**
   * Full task statement given to the agent.
   */
  task_text: string | null;
  steps: StepDetail[];
  edges: Edge[];
  final_answer: unknown;
  /**
   * True when the model-derived run fields are authored.
   */
  fixture: boolean;
}
/**
 * Everything recorded about one step. The same shape serves list and detail views.
 */
export interface StepDetail {
  addr: string;
  seq: number;
  kind: "llm" | "tool" | "retrieval" | "state" | "value";
  name: string;
  agent_role: string | null;
  input: unknown;
  output: unknown;
  /**
   * Reasoning text per call; kept as evidence only.
   */
  reasoning: string[];
  state_before: {
    [k: string]: unknown;
  };
  state_after: {
    [k: string]: unknown;
  };
  /**
   * State keys read, as 'key@vN'.
   */
  reads: string[];
  /**
   * State keys written, as 'key@vN'.
   */
  writes: string[];
  /**
   * False for pure state steps with no LLM/tool call.
   */
  has_call: boolean;
  cache_status: "cached" | "invalidated" | "live" | "edited";
  tokens: TokenUsage | null;
  latency_ms: number;
  finish_reason: string | null;
  retries: number;
  error: StepError | null;
  rule_violations: RuleViolation[];
  hashes: StepHashes;
}
export interface TokenUsage {
  input: number;
  output: number;
  cached: number;
}
export interface StepError {
  type: string;
  message: string | null;
  /**
   * True when later steps ran normally; null when unknown.
   */
  recovered: boolean | null;
}
/**
 * Content hashes (hex SHA-256) of the stored payloads, for exact-match reasoning.
 */
export interface StepHashes {
  request_key: string | null;
  input: string | null;
  output: string | null;
  reasoning: string | null;
  state_before: string;
  state_after: string | null;
}
/**
 * Counts behind the filter chips, computed over the filtered set before paging.
 */
export interface RunFacets {
  agents: {
    [k: string]: number;
  };
  outcomes: {
    [k: string]: number;
  };
  origins: {
    [k: string]: number;
  };
  splits: {
    [k: string]: number;
  };
}
/**
 * GET /runs. Fork sample runs (`origin: fork`) are left out unless filtered for.
 */
export interface RunList {
  total: number;
  limit: number;
  offset: number;
  /**
   * Offset of the next page, or null on the last page.
   */
  next_offset: number | null;
  items: RunSummary[];
  facets: RunFacets;
  /**
   * True when risk, top_suspect and failure_signature are authored illustrations.
   */
  fixture: boolean;
}
/**
 * Query parameters of GET /runs.
 */
export interface RunListQuery {
  agent?: string | null;
  outcome?: ("passed" | "failed" | "running") | null;
  origin?: ("natural" | "injected" | "fork" | "imported") | null;
  split?: ("S0" | "S1" | "S3" | "S4" | "S5") | null;
  label_source?: ("injected" | "natural_auto" | "human" | "verified") | null;
  failure_signature?: string | null;
  /**
   * Free text over task/run id.
   */
  q?: string | null;
  sort?: "started_at" | "duration_ms" | "risk" | "steps";
  order?: "asc" | "desc";
  limit?: number;
  offset?: number;
}
/**
 * An offline TripCrew task submitted as natural language.
 */
export interface TaskRunRequest {
  prompt: string;
  inject_stale_fx?: boolean;
}
export interface TaskRunResponse {
  run_id: string;
  status: "passed" | "failed" | "running";
  task: string;
  steps: number;
}
/**
 * GET /runs/{id}/provenance. Never invents a single origin for an ambiguous value.
 */
export interface ValueProvenance {
  run_id: string;
  target: ValueRef;
  /**
   * The exact origin; set only when there is exactly one candidate.
   */
  producer: ProducerCandidate | null;
  candidates: ProducerCandidate[];
  ambiguous: boolean;
  consumers: ConsumerHop[];
  reaches_final_answer: boolean;
}
export interface ValueRef {
  addr: string;
  json_pointer: string;
  value: unknown;
  value_hash: string | null;
}
export interface VerifyCandidate {
  addr: string;
  status: "queued" | "running" | "complete" | "failed";
  fork_id: string | null;
  edit: ForkEdit | null;
  edit_source: ("schema_rule" | "oracle" | "nearest_twin" | "precedent" | "llm" | "manual") | null;
  fix: PassRate | null;
  control_result: PassRate | null;
  verdict: ("VERIFIED" | "REFUTED" | "INCONCLUSIVE") | null;
  note: string | null;
}
/**
 * POST /runs/{id}/verify (202) and GET /jobs/{job_id}.
 */
export interface VerifyJob {
  job_id: string;
  run_id: string;
  status: "queued" | "running" | "complete" | "failed";
  samples: number;
  candidates: VerifyCandidate[];
  /**
   * Strongest VERIFIED candidate, if any.
   */
  best_addr: string | null;
  created_at: string;
  completed_at: string | null;
  error: ErrorBody | null;
}
/**
 * POST /runs/{id}/verify. The verifier picks the fix for each candidate itself.
 */
export interface VerifyRequest {
  suspects?: [] | [string] | [string, string] | [string, string, string] | null;
  /**
   * K paired samples. Below 4, the Wilson intervals of K/K and 0/K always overlap, so no candidate could ever be VERIFIED.
   */
  samples?: number;
}

// Named enumerations: runtime value lists plus the derived union types.
export const AbstainReasonValues = ["margin_too_small","set_too_large","no_signal","model_unavailable"] as const;
export type AbstainReason = (typeof AbstainReasonValues)[number];
export const AlignModeValues = ["address","lcs"] as const;
export type AlignMode = (typeof AlignModeValues)[number];
export const BranchValues = ["fix","control"] as const;
export type Branch = (typeof BranchValues)[number];
export const CacheStatusValues = ["cached","invalidated","live","edited"] as const;
export type CacheStatus = (typeof CacheStatusValues)[number];
export const ClaimSourceValues = ["template","llm"] as const;
export type ClaimSource = (typeof ClaimSourceValues)[number];
export const ClientKindValues = ["llm","deterministic_standin","imported"] as const;
export type ClientKind = (typeof ClientKindValues)[number];
export const DamageTagValues = ["root","symptom","unaffected"] as const;
export type DamageTag = (typeof DamageTagValues)[number];
export const DiffStatusValues = ["same","cached","changed","new","removed"] as const;
export type DiffStatus = (typeof DiffStatusValues)[number];
export const EdgeKindValues = ["state","message","inferred"] as const;
export type EdgeKind = (typeof EdgeKindValues)[number];
export const EditKindValues = ["override_output","patch_tool_args","patch_prompt","swap_model","patch_tool_result"] as const;
export type EditKind = (typeof EditKindValues)[number];
export const EditSourceValues = ["schema_rule","oracle","nearest_twin","precedent","llm","manual"] as const;
export type EditSource = (typeof EditSourceValues)[number];
export const ErrorCodeValues = ["bad_request","validation_error","not_found","conflict","live_call_refused","replay_divergence","not_verified","not_applicable","rate_limited","unavailable","unsupported","internal_error"] as const;
export type ErrorCode = (typeof ErrorCodeValues)[number];
export const EvalStatusValues = ["ran","not_run"] as const;
export type EvalStatus = (typeof EvalStatusValues)[number];
export const FaultFamilyValues = ["tool","retrieval","decision","coordination"] as const;
export type FaultFamily = (typeof FaultFamilyValues)[number];
export const ForkStatusValues = ["queued","running","complete","error"] as const;
export type ForkStatus = (typeof ForkStatusValues)[number];
export const JobStatusValues = ["queued","running","complete","failed"] as const;
export type JobStatus = (typeof JobStatusValues)[number];
export const LabelSourceValues = ["injected","natural_auto","human","verified"] as const;
export type LabelSource = (typeof LabelSourceValues)[number];
export const MethodKindValues = ["ranker","baseline","llm_judge","literature"] as const;
export type MethodKind = (typeof MethodKindValues)[number];
export const ModeValues = ["live","recorded","offline"] as const;
export type Mode = (typeof ModeValues)[number];
export const ModelBackendValues = ["lightgbm","sklearn-hgb"] as const;
export type ModelBackend = (typeof ModelBackendValues)[number];
export const ReplayModeValues = ["cone","prefix","full"] as const;
export type ReplayMode = (typeof ReplayModeValues)[number];
export const RunModeValues = ["live","recorded","offline","replay"] as const;
export type RunMode = (typeof RunModeValues)[number];
export const RunOriginValues = ["natural","injected","fork","imported"] as const;
export type RunOrigin = (typeof RunOriginValues)[number];
export const RunStatusValues = ["passed","failed","running"] as const;
export type RunStatus = (typeof RunStatusValues)[number];
export const SchemaRuleValues = ["empty_query","empty_result","swallowed_error","repeated_call","schema_invalid","non_finite_or_out_of_range","unit_token_mismatch","stale_as_of","error_status_in_output","other"] as const;
export type SchemaRule = (typeof SchemaRuleValues)[number];
export const SeverityValues = ["error","warning","info"] as const;
export type Severity = (typeof SeverityValues)[number];
export const SplitValues = ["S0","S1","S3","S4","S5"] as const;
export type Split = (typeof SplitValues)[number];
export const StageValues = ["localize","attribute","propose","verify"] as const;
export type Stage = (typeof StageValues)[number];
export const StageStateValues = ["pending","active","done","abstained","blocked"] as const;
export type StageState = (typeof StageStateValues)[number];
export const StateKeyStatusValues = ["unchanged","changed","added","removed"] as const;
export type StateKeyStatus = (typeof StateKeyStatusValues)[number];
export const StepKindValues = ["llm","tool","retrieval","state","value"] as const;
export type StepKind = (typeof StepKindValues)[number];
export const StepPhaseValues = ["queued","running","done","diverged"] as const;
export type StepPhase = (typeof StepPhaseValues)[number];
export const SuspectFlagValues = ["visible_failure","error_recovered","schema_violation","first_anomaly","on_damage_path"] as const;
export type SuspectFlag = (typeof SuspectFlagValues)[number];
export const TimelineKindValues = ["original","control","fork"] as const;
export type TimelineKind = (typeof TimelineKindValues)[number];
export const VerdictValues = ["VERIFIED","REFUTED","INCONCLUSIVE"] as const;
export type Verdict = (typeof VerdictValues)[number];

// Named unions of generated interfaces.
export type ForkEvent = StepEvent | OutcomeEvent | SummaryEvent | StreamErrorEvent;

export interface EndpointSpec {
  method: string;
  path: string;
  summary: string;
  request: string | null;
  response: string;
  statuses: readonly number[];
  query: readonly string[];
  extension: boolean;
}

export const ENDPOINTS: readonly EndpointSpec[] = [
  {
    "method": "GET",
    "path": "/health",
    "summary": "Liveness, mode and capabilities",
    "request": null,
    "response": "AppHealth",
    "statuses": [
      200
    ],
    "query": [],
    "extension": true
  },
  {
    "method": "GET",
    "path": "/agents",
    "summary": "Agents with run counts",
    "request": null,
    "response": "AgentList",
    "statuses": [
      200
    ],
    "query": [],
    "extension": true
  },
  {
    "method": "POST",
    "path": "/tasks/run",
    "summary": "Record an offline TripCrew task from a bounded natural-language prompt",
    "request": "TaskRunRequest",
    "response": "TaskRunResponse",
    "statuses": [
      201,
      409,
      422,
      503
    ],
    "query": [],
    "extension": true
  },
  {
    "method": "GET",
    "path": "/runs",
    "summary": "Paged run list",
    "request": "RunListQuery",
    "response": "RunList",
    "statuses": [
      200,
      422
    ],
    "query": [
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
      "offset"
    ],
    "extension": false
  },
  {
    "method": "GET",
    "path": "/failure-groups",
    "summary": "Failed runs grouped by signature",
    "request": null,
    "response": "FailureGroupList",
    "statuses": [
      200
    ],
    "query": [
      "agent",
      "split"
    ],
    "extension": true
  },
  {
    "method": "GET",
    "path": "/runs/{run_id}",
    "summary": "Run detail with steps and edges",
    "request": null,
    "response": "RunDetail",
    "statuses": [
      200,
      404
    ],
    "query": [
      "blind"
    ],
    "extension": false
  },
  {
    "method": "GET",
    "path": "/runs/{run_id}/steps/{addr}",
    "summary": "One step with full payloads",
    "request": null,
    "response": "StepDetail",
    "statuses": [
      200,
      404
    ],
    "query": [],
    "extension": false
  },
  {
    "method": "GET",
    "path": "/runs/{run_id}/provenance",
    "summary": "Producer candidates and consumer path of one value",
    "request": null,
    "response": "ValueProvenance",
    "statuses": [
      200,
      404,
      422
    ],
    "query": [
      "addr",
      "pointer"
    ],
    "extension": false
  },
  {
    "method": "GET",
    "path": "/runs/{run_id}/diagnosis",
    "summary": "Cached diagnosis, computed on first request",
    "request": null,
    "response": "Diagnosis",
    "statuses": [
      200,
      404,
      409,
      503
    ],
    "query": [
      "refresh"
    ],
    "extension": false
  },
  {
    "method": "POST",
    "path": "/runs/{run_id}/verify",
    "summary": "Start the paired-replay verifier",
    "request": "VerifyRequest",
    "response": "VerifyJob",
    "statuses": [
      202,
      404,
      409,
      422,
      429
    ],
    "query": [],
    "extension": false
  },
  {
    "method": "GET",
    "path": "/jobs/{job_id}",
    "summary": "Poll a verification job",
    "request": null,
    "response": "VerifyJob",
    "statuses": [
      200,
      404
    ],
    "query": [],
    "extension": true
  },
  {
    "method": "POST",
    "path": "/replay/predict",
    "summary": "Predict what a fork will re-run",
    "request": "ForkRequest",
    "response": "ReplayPrediction",
    "statuses": [
      200,
      404,
      422
    ],
    "query": [],
    "extension": true
  },
  {
    "method": "POST",
    "path": "/forks",
    "summary": "Create an immutable fork and start replaying",
    "request": "ForkRequest",
    "response": "ForkCreated",
    "statuses": [
      202,
      404,
      409,
      422,
      429
    ],
    "query": [],
    "extension": false
  },
  {
    "method": "GET",
    "path": "/forks/{fork_id}",
    "summary": "One fork with its paired result",
    "request": null,
    "response": "ForkSummary",
    "statuses": [
      200,
      404
    ],
    "query": [],
    "extension": true
  },
  {
    "method": "GET",
    "path": "/forks/{fork_id}/stream",
    "summary": "Server-sent events of one fork",
    "request": null,
    "response": "text/event-stream",
    "statuses": [
      200,
      404
    ],
    "query": [],
    "extension": false
  },
  {
    "method": "GET",
    "path": "/runs/{run_id}/forks",
    "summary": "Original -> control -> fork timeline",
    "request": null,
    "response": "ForkTimeline",
    "statuses": [
      200,
      404
    ],
    "query": [],
    "extension": true
  },
  {
    "method": "GET",
    "path": "/diff",
    "summary": "Aligned comparison of two runs",
    "request": null,
    "response": "DiffResponse",
    "statuses": [
      200,
      404,
      422
    ],
    "query": [
      "a",
      "b"
    ],
    "extension": false
  },
  {
    "method": "GET",
    "path": "/runs/{run_id}/twin",
    "summary": "Nearest passing twin",
    "request": null,
    "response": "NearestTwin",
    "statuses": [
      200,
      404
    ],
    "query": [],
    "extension": true
  },
  {
    "method": "GET",
    "path": "/runs/{run_id}/report",
    "summary": "Structured crash report",
    "request": null,
    "response": "CrashReport",
    "statuses": [
      200,
      404,
      409
    ],
    "query": [],
    "extension": true
  },
  {
    "method": "GET",
    "path": "/runs/{run_id}/report.md",
    "summary": "Crash report as Markdown",
    "request": null,
    "response": "text/markdown",
    "statuses": [
      200,
      404,
      409
    ],
    "query": [],
    "extension": true
  },
  {
    "method": "POST",
    "path": "/forks/{fork_id}/export-test",
    "summary": "Generate an offline regression test",
    "request": "ExportTestRequest",
    "response": "ExportTestResponse",
    "statuses": [
      200,
      404,
      409
    ],
    "query": [],
    "extension": false
  },
  {
    "method": "GET",
    "path": "/eval",
    "summary": "Precomputed evaluation",
    "request": null,
    "response": "EvalResponse",
    "statuses": [
      200,
      404
    ],
    "query": [],
    "extension": false
  },
  {
    "method": "GET",
    "path": "/labels/queue",
    "summary": "Failed runs awaiting a human label",
    "request": null,
    "response": "LabelQueue",
    "statuses": [
      200
    ],
    "query": [
      "annotator",
      "limit"
    ],
    "extension": true
  },
  {
    "method": "POST",
    "path": "/labels",
    "summary": "Record a human label",
    "request": "LabelRequest",
    "response": "LabelResponse",
    "statuses": [
      201,
      404,
      409,
      422
    ],
    "query": [],
    "extension": false
  },
  {
    "method": "POST",
    "path": "/v1/traces",
    "summary": "OTLP/HTTP JSON trace ingest",
    "request": "OTLP ExportTraceServiceRequest",
    "response": "OtlpIngestAck",
    "statuses": [
      200,
      400,
      422
    ],
    "query": [],
    "extension": false
  },
];
