"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { UNREACHABLE_HINT, api, errorText } from "../../lib/api";
import { useAppContext } from "../shell/AppContext";

// Each example was checked end to end with the old exchange rate: the run fails only on the
// INR total, the diagnoser ranks fx_rate first, and the paired fix test comes back VERIFIED.
// Prices are seeded from trip constraints; changing only the budget leaves them unchanged.
const EXAMPLES = [
  { route: "Delhi to Tokyo", prompt: "Plan a trip from Delhi to Tokyo departing 2026-12-12, returning 2026-12-17, for 2 adults. Budget ₹1,00,000." },
  { route: "Chennai to Bangkok", prompt: "Family holiday from Chennai to Bangkok, 2026-12-20 to 2026-12-24, 3 adults, budget ₹1,40,000. Vegetarian: yes." },
  { route: "Hyderabad to Dubai", prompt: "Weekend from Hyderabad to Dubai departing 2026-12-05, returning 2026-12-08, for 2 adults. Budget Rs. 1,00,000. No red-eye: yes." },
  { route: "Bangalore to London", prompt: "Solo trip from Bangalore to London departing 2026-12-01, returning 2026-12-07, for 1 adult. Budget ₹80,000. Refundable: yes." },
];
const SAMPLE = EXAMPLES[0].prompt;

type TaskRunResponse = { run_id: string; status: "passed" | "failed"; task: string; steps: number };

export function NewRunPage() {
  const router = useRouter();
  const { mode, staticBundle, reachable } = useAppContext();
  const [prompt, setPrompt] = useState(SAMPLE);
  const [injectStaleFx, setInjectStaleFx] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const result = await api<TaskRunResponse>("/tasks/run", {
        method: "POST",
        body: JSON.stringify({ prompt, inject_stale_fx: injectStaleFx }),
      });
      router.push(`/investigate/${encodeURIComponent(result.run_id)}`);
    } catch (caught) {
      setError(errorText(caught) || "The task could not be run.");
      setBusy(false);
    }
  }

  const enabled = reachable && mode === "offline" && !staticBundle;
  const unavailable = !reachable
    ? `The API is not reachable. ${UNREACHABLE_HINT}`
    : staticBundle
      ? "This is the static recorded showcase, so new runs cannot be recorded. Start the local API to run tasks."
      : mode === null
        ? "Connecting to the API…"
        : mode !== "offline"
          ? `The API is running in ${mode.toUpperCase()} mode. Custom tasks use the deterministic local runner: set MODE=offline in .env and restart make dev-api.`
          : "";

  return (
    <div className="page">
      <div className="page-inner new-run-page">
        <h1 className="h1">Plan a trip</h1>
        <p className="muted new-run-intro">Enter a request. Inspect each step after the run.</p>

        <form className="new-run-form card card-pad" onSubmit={submit}>
          <label className="label" htmlFor="task-prompt">Your request</label>
          <textarea
            id="task-prompt"
            className="input prompt-input"
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            maxLength={1200}
            required
            minLength={24}
            spellCheck={false}
            placeholder={SAMPLE}
          />
          <div className="spread prompt-meta">
            <span className="faint">{prompt.length}/1200</span>
            <button type="button" className="btn btn-sm" onClick={() => setPrompt(SAMPLE)}>Reset example</button>
          </div>

          <div className="prompt-examples" role="group" aria-label="Example trips with the exchange-rate bug">
            <span className="faint">Examples with the exchange-rate bug</span>
            {EXAMPLES.map((example) => (
              <button type="button" key={example.route} className={`btn btn-sm${prompt === example.prompt && injectStaleFx ? " btn-chosen" : ""}`}
                onClick={() => { setPrompt(example.prompt); setInjectStaleFx(true); }}>
                {example.route}
              </button>
            ))}
          </div>

          <div className="prompt-help">Fly from Mumbai, Delhi, Bengaluru, Chennai or Hyderabad to Singapore, Bangkok, Dubai, London, Tokyo or Paris. Give dates as YYYY-MM-DD, 1 to 6 travellers, and a budget in rupees.</div>

          <label className="prompt-checkbox">
            <input type="checkbox" checked={injectStaleFx} onChange={(event) => setInjectStaleFx(event.target.checked)} />
            <span><strong>Use an old exchange rate</strong><small>Add a reproducible budget error.</small></span>
          </label>

          {error && <p className="error-box" role="alert">{error}</p>}
          {unavailable && <p className="muted" role="status">{unavailable}</p>}
          <div className="row prompt-actions">
            <span className="spacer" />
            <button className="btn btn-primary" type="submit" disabled={!enabled || busy || prompt.trim().length < 24}>
              {busy ? "Recording run…" : "Run and inspect"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
