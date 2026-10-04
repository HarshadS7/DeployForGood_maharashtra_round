#!/usr/bin/env bash
# Rebuild the offline TripCrew dataset end to end: base runs, injected faults, natural
# failures, frozen labels, then train and evaluate the diagnoser. Deterministic and
# network-free. The diagnoser is trained on TripCrew only.
set -euo pipefail

RUN=(uv run --locked --extra dev --extra ml --extra server python)
# Fresh-FX scenario seeds give passing base runs (Fault Forge injection sites); stale-FX
# seeds give natural budget failures. Scenario IDs are TC-<seed>-<n>, so seeds never collide.
FRESH_SEEDS=(7 13 17)
STALE_SEEDS=(11 19)
FRESH_COUNT=${FRESH_COUNT:-120}
STALE_COUNT=${STALE_COUNT:-40}
QUOTA_TRIPCREW=${QUOTA_TRIPCREW:-60}
STALE_CSV=$(IFS=,; echo "${STALE_SEEDS[*]}")

if [[ "${1:-}" == "--fresh" ]]; then
  rm -rf data/tripcrew data/eval data/models
fi

echo "== TripCrew base runs (fresh FX)"
for seed in "${FRESH_SEEDS[@]}"; do
  "${RUN[@]}" -m agents.tripcrew run --count "$FRESH_COUNT" --seed "$seed" \
    --report "data/tripcrew/report-$seed.json"
done

echo "== TripCrew natural stale-FX failures"
for seed in "${STALE_SEEDS[@]}"; do
  "${RUN[@]}" -m agents.tripcrew run --count "$STALE_COUNT" --seed "$seed" --stale-fx \
    --report "data/tripcrew/report-stale-$seed.json"
done

echo "== Fault Forge: injected faults"
"${RUN[@]}" -m blackbox.forge inject --agent tripcrew --quota "$QUOTA_TRIPCREW" \
  --concurrency 4 --stale-fx-seeds "$STALE_CSV"

echo "== Natural failures (test-only, labelled by oracle counterfactual replay)"
"${RUN[@]}" -m blackbox.forge natural --agent tripcrew --stale-fx-seeds "$STALE_CSV"

echo "== Freeze"
"${RUN[@]}" -m blackbox.forge freeze --agent tripcrew

echo "== Replay settings the API needs to rebuild the agent"
echo "{\"stale_fx_seeds\": [$STALE_CSV]}" > data/tripcrew/replay.json

echo "== Train and evaluate"
"${RUN[@]}" -m blackbox.ml eval
