"use client";

import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import type { EvalResponse } from "../../lib/contract";

export function ResultsPage() {
  const [data, setData] = useState<EvalResponse | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    api<EvalResponse>("/eval").then(setData).catch((error) => setErr(error.message));
  }, []);

  return (
    <div className="page">
      <div className="page-inner results-page">
        <h1 className="h1">How well does it find the failing step?</h1>
        {data && !data.fixture && headline(data) && <p className="results-headline">{headline(data)}</p>}
        {!data && !err && <p className="muted results-intro">Loading the evaluation…</p>}
        {err && <p className="error-box" role="alert">{err}</p>}
        {data && (
          <>
            {data.fixture && <div className="fixture-note">Illustrative data · run evaluation for measured results.</div>}
            <div className="grid-cards results-metrics">
              {data.headline.map((card) => (
                <div className="metric" key={card.id}>
                  <div className="label">{metricTitle[card.id] || card.title}</div>
                  <div className="value">
                    {card.value
                      ? card.unit === "fraction"
                        ? pct(card.value.value)
                        : `${card.value.value}${card.unit === "ms" ? " ms" : ""}`
                      : "—"}
                  </div>
                  {(card.n != null || card.comparator) && <p className="faint">
                    {card.n != null ? `${card.n} samples` : ""}
                    {card.comparator && `${card.n != null ? " · " : ""}baseline ${pct(card.comparator.value)}`}
                  </p>}
                </div>
              ))}
            </div>

            <details className="evaluation-details">
              <summary>Full evaluation</summary>
              <div className="evaluation-content">
                <h2 className="h2">Localization by method</h2>
                <div className="card table-wrap evaluation-table">
                  <table className="table">
                    <thead><tr><th>Method</th><th>Test set</th><th>Runs</th><th>First guess right</th><th>In top 3</th><th>Within 1 step</th><th>MRR</th></tr></thead>
                    <tbody>{data.leaderboard.map((row, index) => (
                      <tr key={`${row.method}-${row.split}-${index}`}>
                        <td>{methodName(row.method)}{row.reported_literature && <span className="badge badge-neutral">paper</span>}</td>
                        <td title={row.split}>{splitName(row.split)}</td><td>{row.n ?? "–"}</td>
                        <td>{pct(row.top1?.value)}</td><td>{pct(row.top3?.value)}</td>
                        <td>{pct(row.within1?.value)}</td><td>{pct(row.mrr?.value)}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>

                <div className="grid-two evaluation-ablations">
                  {data.recorder_ablations.map((ablation) => (
                    <section className="card card-pad" key={ablation.id}>
                      <h2 className="h2">{ablation.title}</h2>
                      <p className="muted">{splitName(ablation.split)}: {ablation.interpretation?.replaceAll("_", " ") || "incomplete"}</p>
                      {ablation.arms.map((arm) => (
                        <div className="bar-row" key={arm.arm}>
                          <span>{arm.label}</span>
                          <div className="bar"><span style={{ width: `${(arm.top1?.value || 0) * 100}%`, background: "var(--accent)" }} /></div>
                          <strong>{pct(arm.top1?.value)}</strong>
                        </div>
                      ))}
                    </section>
                  ))}
                </div>

                <section className="card card-pad evaluation-reliability">
                  <h2 className="h2">Coverage and limits</h2>
                  <p>Calibration: ECE {data.calibration.ece?.toFixed(3) ?? "—"}; abstention {pct(data.calibration.abstain_rate)}.</p>
                  {data.not_run.length > 0 && <ul>{data.not_run.map((item) => <li key={`${item.section}:${item.item}`}><strong>{item.item}</strong>: {item.reason}</li>)}</ul>}
                </section>
                <p className="faint evaluation-generated">Updated {new Date(data.generated_at).toLocaleString()} · Dataset {data.dataset.version}</p>
              </div>
            </details>
          </>
        )}
      </div>
    </div>
  );
}

/** One plain sentence: unseen-fault accuracy against the strongest baseline. */
function headline(data: EvalResponse): string | null {
  const card = data.headline.find((item) => item.id === "unseen_fault_top1");
  if (!card?.value) return null;
  const ours = pct(card.value.value);
  return card.comparator
    ? `On fault types it never saw in training, Black Box names the failing step first ${ours} of the time. The best simple baseline manages ${pct(card.comparator.value)}.`
    : `On fault types it never saw in training, Black Box names the failing step first ${ours} of the time.`;
}

const METHODS: Record<string, string> = {
  blackbox: "Black Box",
  random: "Random step",
  "last-step": "Always the last step",
  "first-error": "First step with an error",
  "anomaly-max": "Most unusual step",
  "position-only": "Step position only",
};

const SPLITS: Record<string, string> = {
  S0: "Seen fault types",
  S1: "Unseen fault types",
  S3: "Unseen agent",
  S4: "Natural failures",
  S5: "Zero-shot",
};

function methodName(method: string) {
  return METHODS[method] ?? method;
}

/** "S1" -> "Unseen fault types"; "S3:tripcrew" -> "Unseen agent (tripcrew)". */
function splitName(split: string) {
  const [code, detail] = split.split(":");
  const name = SPLITS[code] ?? code;
  return detail ? `${name} (${detail})` : name;
}

const metricTitle: Record<string, string> = {
  unseen_fault_top1: "First guess · unseen failures",
  natural_top1: "First guess · natural failures",
  replay_savings: "Replay calls avoided",
  ms_per_trace: "Diagnosis time",
};

function pct(value?: number | null) {
  return value == null ? "—" : `${(value * 100).toFixed(1)}%`;
}
