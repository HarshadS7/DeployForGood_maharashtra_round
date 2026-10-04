# TripCrew agent

TripCrew is a runnable travel-planning agent using the recorder and replay SDK.
All travel data is synthetic. The five tools only read the local generated
catalog; they do not book travel or query external travel services.

## Run it

```sh
make tripcrew
make tripcrew-demo
uv run --locked --extra dev python -m agents.tripcrew scenarios \
  --count 300 --seed 7 --report data/tripcrew/scenarios.json
```

`run` defaults to 20 scenarios, seed 7, and the deterministic fixture client.
`demo` defaults to one deliberately stale-FX scenario and an immutable replay
with `patch_tool_args("fx/tool#1", {"fresh": True})`. The console prints both
outcomes; the JSON report includes the original and repaired plans and every
step's replay status. Use `--data-dir` and `--report` to select other destinations.
No key or network connection is needed for either command.

For real model calls, configure `GROQ_API_KEY` in the local `.env`, then run:

```sh
uv run --locked --extra dev python -m agents.tripcrew run \
  --client groq --count 20 --report data/tripcrew/live.json
```

The live path uses the configured `AGENT_MODEL` (currently `openai/gpt-oss-20b`),
the shared rate-limit-aware client, structured JSON prompts, and the same graph,
tools and checker as offline. There are seven model calls per successful run.
Invalid model output is recorded as a failed run, never replaced with an oracle
answer. Reports name the client and model to distinguish fixture results from
real model measurements. Do not commit reports or credentials.

Reports are saved atomically before execution, after each run, and on interruption.
The suite and each row carry a `status`; a timeout or cancellation retains the
completed results and identifies the interrupted run. `count` is the number of
attempted runs, while `completed_count` is the pass-rate denominator. Interrupted
infrastructure calls are excluded from model accuracy. Network errors still stop
the command with a nonzero exit status.

## Implementation

- `scenarios.py`: 300 seeded templates across five Indian origins, six destinations,
  dates, adult counts, budgets and combinations of vegetarian/refundable/no-red-eye
  constraints. Every scenario has a feasible cheapest solution and a precomputed
  fresh-catalog total. Catalog prices use a stable per-scenario seed.
- `mock_apis.py`: flight/hotel search, weather, FX and visa rules, each with `as_of`;
  `fresh=True` bypasses the optional synthetic stale quote. Normal runs use fresh
  data; `--stale-fx` explicitly opts into the stale fixture.
- `agent.py`: 16 steps and scoped contexts. Flight workers never see hotel, FX or
  weather observations; hotel workers never see flight or FX observations.
- `models.py` and `prompts.py`: strict JSON output contracts and scoped prompts.
- `checker.py`: checks original task identity, dates, travellers, catalog IDs,
  constraints, cheapest feasible selections, budget and an INR error of at most 1.
  Prices and flags are taken from the catalog, not trusted from model output.
- `fixture_client.py`: an explicitly named deterministic test double, with no
  hidden calls or fallback to a live model.

The 16 recorded steps are:

1. `planner/chat#1`: parse the natural-language task.
2. `planner/state#1`: route only relevant constraints to each worker.
3. In parallel: `flight/chat#1`, `hotel/chat#1`, `weather/tool#1`, `fx/tool#1`, `visa/tool#1`.
4. In parallel: `flight/tool#1`, `hotel/tool#1`.
5. In parallel: `flight/chat#2`, `hotel/chat#2` select catalog entries.
6. `budget/tool#1`: deterministic calculation using the observed exchange rate.
7. `writer/chat#1`, then `verifier/chat#1`.
8. `final/state#1`, then `checker/state#1` produce and score the final plan.

Within each parallel stage, all calls complete before results are committed in a
fixed order. Scope entry is also ordered. Thus asynchronous completion times do
not change the pre-edit snapshot hashes during replay. Worker errors cancel
unfinished siblings and propagate without leaving a barrier waiting forever.

The FX repair makes new calls only at FX, budget, writer and verifier. The three
local state-only steps run again as ordinary program code; they make no tool or
model calls. The SDK currently labels these steps `live`, so distinguish them
from call counts. Independent travel calls are cached. Unchanged replay makes
zero new model/tool calls and preserves the final snapshot.

Repeated replay samples preserve the original seed when that produces an exact
match to a recorded model request. Changed model calls and forced controls retain
their paired sample seeds; explicit seed edits are respected. The regression suite
covers five unchanged replays and five edited/control pairs without prefix drift.

## Validation and five worked checks

The automated suite validates all 300 scenarios, runs 20 end to end, checks the
parallel/scoped contexts, exercises malformed responses and replay divergence,
checks every hard constraint, and proves the FX repair leaves the original run
unchanged. The 20 fresh-fixture runs passed 20/20. This is not the real model's
pass rate.

Five independently inspected totals for seed 7 are below. Fares are round-trip
per adult. Rooms = ceil(adults / 2); hotel prices are per room per night. The
formula is flights + hotel × nights × rooms × fresh FX + visa fees, rounded once
to two decimals.

| Scenario | Cheapest feasible IDs | Arithmetic in INR | Expected total |
| --- | --- | --- | ---: |
| TC-7-0001 | F-basic / H-basic | 24,000 × 4 + 70.31 × 3 × 2 × 64 + 3,000 × 4 | 134,999.04 |
| TC-7-0002 | F-basic / H-flex | 15,500 + 2,163.46 × 6 × 2.6 + 3,000 | 52,249.98 |
| TC-7-0003 | F-flex / H-flex | 18,000 × 2 + 339.68 × 6 × 23 + 2,500 × 2 | 87,875.84 |
| TC-7-0004 | F-flex / H-flex | 17,000 + 30.69 × 5 × 112 + 2,000 | 36,186.40 |
| TC-7-0005 | F-flex / H-basic | 26,000 + 5,603.45 × 5 × 0.58 + 2,500 | 44,750.01 |

In order, these scenarios require: no extra constraints; vegetarian; refundable;
vegetarian and refundable; no red-eye. Catalog flags satisfy those choices, and
each generated budget exceeds the corresponding total. These values are also
literal regression expectations in `tests/test_tripcrew.py`.

The stale-FX demo reports 130,949.18 INR, fails the checker, and repairs to
134,999.04 INR when fresh FX is fetched.

## Scope

The New task page parses requests for the supported synthetic catalog. It does not
make live model calls or query real travel inventory. Changing only a request's
budget preserves its generated catalog. Unsupported cities and incomplete requests
return a validation error.

Live model calls are available through `--client groq`. Offline fixture pass rates
are not measurements of that model's performance. Current diagnosis evaluation is
TripCrew-only; natural-failure examples share the stale-FX root cause.

Paired verification compares edited and unchanged branches with matching seeds.
VERIFIED means the edited pass-rate lower 95% Wilson bound exceeds the control's
upper bound for the tested task. Equal failing branches are INCONCLUSIVE. A verified
fix can be exported with its cassettes, state and prompt metadata for offline replay.
