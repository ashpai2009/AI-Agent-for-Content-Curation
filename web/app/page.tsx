"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/* ---------------------------------------------------------------------------------- */
/* Types — the shapes the council service actually returns                              */
/* ---------------------------------------------------------------------------------- */

type JobStatus = {
  job_id: string;
  state: string;
  failure_reason: string | null;
  succeeded: boolean;
  issues: number;
  issues_open: number;
  changes: number;
  llm_calls_used: number;
};

type Finding = {
  code: string;
  severity: string;
  message: string;
  row?: number | null;
  problem_name?: string | null;
};

type Report = {
  state: string;
  succeeded: boolean;
  integrity_passed: boolean;
  changes_applied: number;
  issues_resolved: number;
  issues_repaired: number;
  issues_refuted: number;
  remaining_findings: Finding[];
  integrity_findings: Finding[];
  issues_needing_a_person: { problem_name: string; title: string }[];
  instruction_claims_confirmed: number;
  instruction_claims_refuted: number;
  instruction_claims_unresolved: number;
  unresolved_summary: string;
};

type Health = {
  ready: boolean;
  configured: boolean;
  detail?: string | null;
  subscription_type?: string | null;
  model?: string | null;
};

/* ---------------------------------------------------------------------------------- */
/* Phases                                                                              */
/* ---------------------------------------------------------------------------------- */

/** The pipeline, in the order a curator sees it happen. */
const PHASES: { states: string[]; label: string }[] = [
  { states: ["created", "ingesting"], label: "Reading the workbook" },
  { states: ["auditing"], label: "Auditing every problem block" },
  { states: ["repairing_known"], label: "Repairing what the audit found" },
  { states: ["independent_review"], label: "Independent review of untouched problems" },
  {
    states: ["final_validation", "repairing_validation"],
    label: "Final validation",
  },
  { states: ["finalizing"], label: "Writing the corrected workbook" },
];

const TERMINAL = new Set([
  "succeeded",
  "needs_human_attention",
  "failed",
  "cancelled",
]);

/** How far down the list a state sits; terminal states are past the end. */
function phaseIndex(state: string): number {
  if (TERMINAL.has(state)) return PHASES.length;
  const found = PHASES.findIndex((phase) => phase.states.includes(state));
  return found === -1 ? 0 : found;
}

const PURPOSES: { value: string; label: string; hint: string }[] = [
  {
    value: "auto",
    label: "Work it out per paragraph",
    hint: "Each passage is classified on its own wording. Right for mixed notes.",
  },
  {
    value: "errata",
    label: "Specific problems that are wrong",
    hint: "Checked against the blocks each note could be about, and confirmed or refuted.",
  },
  {
    value: "rules",
    label: "Standing rules for the whole workbook",
    hint: "Applied as policy by the writer and both reviewers, on every call.",
  },
  {
    value: "notes",
    label: "Background, not instructions",
    hint: "Recorded with the job and not acted on.",
  },
];

/** The service caps curator policy so it does not ride on every call unbounded. */
const RULE_CHARACTER_CAP = 4000;

/* ---------------------------------------------------------------------------------- */
/* Page                                                                                */
/* ---------------------------------------------------------------------------------- */

export default function Page() {
  const [health, setHealth] = useState<Health | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [notes, setNotes] = useState("");
  const [purpose, setPurpose] = useState("auto");
  const [dragging, setDragging] = useState(false);

  const [jobId, setJobId] = useState<string | null>(null);
  const [status, setStatus] = useState<JobStatus | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const fileInput = useRef<HTMLInputElement>(null);

  /* -- readiness ------------------------------------------------------------------- */

  useEffect(() => {
    let live = true;
    fetch("/api/health")
      .then((r) => r.json())
      .then((body: Health) => live && setHealth(body))
      .catch(() => live && setHealth({ ready: false, configured: true }));
    return () => {
      live = false;
    };
  }, []);

  /* -- polling --------------------------------------------------------------------- */

  useEffect(() => {
    if (!jobId) return;
    let live = true;

    const tick = async () => {
      try {
        const response = await fetch(`/api/jobs/${jobId}`);
        const body = await response.json();
        if (!live) return;
        if (!response.ok) {
          setError(body.error || "the council service stopped answering");
          return;
        }
        setStatus(body as JobStatus);
        if (TERMINAL.has(body.state)) {
          const reportResponse = await fetch(`/api/jobs/${jobId}/report`);
          if (live && reportResponse.ok) setReport(await reportResponse.json());
          window.clearInterval(timer);
        }
      } catch {
        if (live) setError("lost contact with the council service");
      }
    };

    void tick();
    const timer = window.setInterval(tick, 2000);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [jobId]);

  /* -- submission ------------------------------------------------------------------ */

  const submit = useCallback(async () => {
    if (!file) return;
    setSubmitting(true);
    setError(null);
    setReport(null);
    setStatus(null);

    const form = new FormData();
    form.set("workbook", file);
    form.set("instructions_text", notes);
    form.set("instructions_purpose", purpose);

    try {
      const response = await fetch("/api/jobs", { method: "POST", body: form });
      const body = await response.json();
      if (!response.ok) {
        setError(body.error || "the workbook was not accepted");
        return;
      }
      setJobId(body.job_id);
    } catch {
      setError("could not reach this site's own API route");
    } finally {
      setSubmitting(false);
    }
  }, [file, notes, purpose]);

  const reset = () => {
    setJobId(null);
    setStatus(null);
    setReport(null);
    setError(null);
    setFile(null);
    if (fileInput.current) fileInput.current.value = "";
  };

  const choose = (chosen: File | null | undefined) => {
    if (!chosen) return;
    if (!chosen.name.toLowerCase().endsWith(".xlsx")) {
      setError("the workbook must be a .xlsx file");
      return;
    }
    setError(null);
    setFile(chosen);
  };

  const running = Boolean(jobId) && !(status && TERMINAL.has(status.state));
  const current = status ? phaseIndex(status.state) : -1;

  return (
    <main>
      <header className="masthead">
        <h1>Curation Council</h1>
        <StatusPill health={health} />
      </header>
      <p className="lede">
        Upload an OATutor problem workbook. Four agents audit every problem block, repair
        what they find, review the repairs, and hand back a corrected file with a record
        of every change. Your original is never modified.
      </p>

      {error && (
        <div className="notice">
          <p>{error}</p>
        </div>
      )}

      {health && !health.ready && (
        <div className="notice">
          <p>The council service is not ready, so a job would sit unprocessed.</p>
          {health.detail && <p>{health.detail}</p>}
        </div>
      )}

      {!jobId ? (
        <>
          <section className="card">
            <h2>
              <span className="step-number">1</span> The workbook
            </h2>
            <p className="hint">
              A single-sheet <code>.xlsx</code> in the OATutor format. It is copied before
              anything is edited, and the copy is what gets changed.
            </p>
            <div
              className={`dropzone${dragging ? " dragging" : ""}${file ? " chosen" : ""}`}
              role="button"
              tabIndex={0}
              onClick={() => fileInput.current?.click()}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  fileInput.current?.click();
                }
              }}
              onDragOver={(event) => {
                event.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(event) => {
                event.preventDefault();
                setDragging(false);
                choose(event.dataTransfer.files?.[0]);
              }}
            >
              {file ? (
                <>
                  <div className="file-name">{file.name}</div>
                  <div className="file-meta">
                    {(file.size / 1024).toFixed(0)} KB — click to choose a different file
                  </div>
                </>
              ) : (
                <>
                  <div className="file-name">Choose a workbook</div>
                  <div className="file-meta">or drag one here</div>
                </>
              )}
            </div>
            <input
              ref={fileInput}
              type="file"
              accept=".xlsx"
              hidden
              onChange={(event) => choose(event.target.files?.[0])}
            />
          </section>

          <section className="card">
            <h2>
              <span className="step-number">2</span> Anything you want to add
              <span className="file-meta" style={{ fontWeight: 400 }}>
                optional
              </span>
            </h2>
            <p className="hint">
              The standing curation rules — notation, dependencies, row types, LaTeX,
              multiple choice — are already built in and applied on every call. You do not
              need to restate them. Use this for what is specific to <em>this</em>{" "}
              workbook: a problem you know is wrong, or a convention your department
              follows.
            </p>
            <textarea
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
              placeholder={
                "e.g.\nProblem angles3 has the wrong answer on its second step.\n" +
                "Answers in this workbook should be written with spelled-out pi, never a symbol."
              }
              aria-label="Extra instructions"
            />
            <div className="field">
              <label htmlFor="purpose">Treat this as</label>
              <select
                id="purpose"
                value={purpose}
                onChange={(event) => setPurpose(event.target.value)}
              >
                {PURPOSES.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              <span
                className={`counter${
                  purpose === "rules" && notes.length > RULE_CHARACTER_CAP ? " over" : ""
                }`}
              >
                {notes.length.toLocaleString()} characters
              </span>
            </div>
            <p className="hint" style={{ marginTop: 10 }}>
              {PURPOSES.find((option) => option.value === purpose)?.hint}{" "}
              {purpose === "rules" && notes.length > RULE_CHARACTER_CAP && (
                <strong>
                  Rules are capped at {RULE_CHARACTER_CAP.toLocaleString()} characters
                  because they ride on every call; the rest will be cut.
                </strong>
              )}
            </p>
            <p className="hint">
              Whatever you write here is treated as <em>content</em>, fenced off and
              labelled. It can inform the agents; it cannot rewrite their instructions.
            </p>
          </section>

          <div className="actions">
            <button
              className="primary"
              onClick={submit}
              disabled={!file || submitting || (health ? !health.ready : false)}
            >
              {submitting ? "Starting…" : "Start curation"}
            </button>
            {file && (
              <button className="link" onClick={() => setFile(null)}>
                clear
              </button>
            )}
          </div>
        </>
      ) : (
        <>
          <section className="card">
            <h2>{running ? "Running" : "Finished"}</h2>
            <p className="hint">
              Job <code>{jobId.slice(0, 8)}</code>
              {running && " — this takes a few minutes. You can leave the page open."}
            </p>
            <ol className="phases">
              {PHASES.map((phase, index) => {
                const state =
                  index < current ? "done" : index === current ? "current" : "";
                return (
                  <li key={phase.label} className={state}>
                    <span className="marker">
                      {index < current ? "✓" : index === current && running ? (
                        <span className="spinner" />
                      ) : (
                        "·"
                      )}
                    </span>
                    {phase.label}
                  </li>
                );
              })}
            </ol>
            {status && (
              <dl className="stats">
                <div>
                  <dt>Findings tracked</dt>
                  <dd>{status.issues}</dd>
                </div>
                <div>
                  <dt>Cells changed</dt>
                  <dd>{status.changes}</dd>
                </div>
                <div>
                  <dt>Model calls</dt>
                  <dd>{status.llm_calls_used}</dd>
                </div>
              </dl>
            )}
          </section>

          {report && status && (
            <Result jobId={jobId} status={status} report={report} onReset={reset} />
          )}
        </>
      )}

      <footer>
        The agents&apos; own instructions live in the service and are never sent to this
        page. The corrected workbook is a copy; the file you uploaded is left byte for
        byte as it was.
      </footer>
    </main>
  );
}

/* ---------------------------------------------------------------------------------- */
/* Result                                                                              */
/* ---------------------------------------------------------------------------------- */

function Result({
  jobId,
  status,
  report,
  onReset,
}: {
  jobId: string;
  status: JobStatus;
  report: Report;
  onReset: () => void;
}) {
  // Three outcomes, and the difference matters: a finished job, a finished job whose
  // content needs a person, and a job that could not finish. Only the first is success,
  // and the page must never round the other two up to it.
  const tone =
    status.state === "succeeded"
      ? "ok"
      : status.state === "needs_human_attention"
        ? "warn"
        : "bad";
  const headline =
    status.state === "succeeded"
      ? "Finished — nothing left outstanding"
      : status.state === "needs_human_attention"
        ? "Finished, but some of it needs a person"
        : "The job could not finish";

  // Artefacts exist for the first two; a job that failed mid-pipeline may have none.
  const downloadable = status.state === "succeeded" || status.state === "needs_human_attention";

  // Remaining findings are split by severity rather than counted together, because the two
  // halves mean opposite things to a curator. `blocking`/`error` is work left undone.
  // `warning`/`observation` is the council reporting something it deliberately never
  // corrects -- the correct MC answer sitting first, or an optional header this workbook
  // does not carry. Counting them as one number told a curator whose workbook was finished
  // that it was not, which is the failure mode this whole system exists to avoid.
  const openErrors = report.remaining_findings.filter(
    (f) => f.severity === "blocking" || f.severity === "error",
  );
  const observations = report.remaining_findings.filter(
    (f) => f.severity !== "blocking" && f.severity !== "error",
  );

  return (
    <>
      <section className="card">
        <div className={`verdict ${tone}`}>
          <strong>{headline}</strong>
          <span>{report.unresolved_summary}</span>
        </div>

        <dl className="stats">
          <div>
            <dt>Repaired</dt>
            <dd>{report.issues_repaired}</dd>
          </div>
          <div>
            <dt>Refuted</dt>
            <dd>{report.issues_refuted}</dd>
          </div>
          <div>
            <dt>Cells changed</dt>
            <dd>{report.changes_applied}</dd>
          </div>
          <div>
            <dt>Open errors</dt>
            <dd>{openErrors.length}</dd>
          </div>
          <div>
            <dt>Observations</dt>
            <dd>{observations.length}</dd>
          </div>
        </dl>

        {!report.integrity_passed && (
          <div className="notice" style={{ marginTop: 16 }}>
            <p>
              The integrity check did not pass. Something in the output file cannot be
              traced to an approved edit, so do not use it until the findings below are
              understood.
            </p>
          </div>
        )}

        <div className="actions" style={{ marginTop: 18 }}>
          {downloadable && (
            <a className="download" href={`/api/jobs/${jobId}/download`}>
              Download corrected workbook
            </a>
          )}
          <button className="link" onClick={onReset}>
            curate another workbook
          </button>
        </div>
      </section>

      {report.issues_needing_a_person.length > 0 && (
        <section className="card">
          <h2>Needs a person</h2>
          <p className="hint">
            The council tried and stopped rather than guessing. These are the ones to look
            at yourself.
          </p>
          <table className="findings">
            <tbody>
              {report.issues_needing_a_person.map((issue, index) => (
                <tr key={index}>
                  <td style={{ width: "30%" }}>
                    <code>{issue.problem_name}</code>
                  </td>
                  <td>{issue.title}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {openErrors.length > 0 && (
        <Findings
          title="Open errors"
          hint="Still present in the workbook as handed back, and still work to do."
          findings={openErrors}
        />
      )}

      {observations.length > 0 && (
        <Findings
          title="Observations"
          hint="Reported, never auto-corrected, and not a reason to hold the workbook back — a correct answer sitting first among the choices, or an optional column this workbook does not use."
          findings={observations}
        />
      )}

      {report.integrity_findings.length > 0 && (
        <Findings
          title="Integrity"
          hint="Differences between your file and the output that no approved edit accounts for."
          findings={report.integrity_findings}
        />
      )}
    </>
  );
}

function Findings({
  title,
  hint,
  findings,
}: {
  title: string;
  hint: string;
  findings: Finding[];
}) {
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? findings : findings.slice(0, 12);

  return (
    <section className="card">
      <h2>
        {title} <span className="file-meta">{findings.length}</span>
      </h2>
      <p className="hint">{hint}</p>
      <table className="findings">
        <thead>
          <tr>
            <th style={{ width: "16%" }}>Row</th>
            <th style={{ width: "26%" }}>Rule</th>
            <th>What it says</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((finding, index) => (
            <tr key={index}>
              <td>{finding.row ?? "—"}</td>
              <td>
                <code>{finding.code}</code>
                <br />
                <span className={`severity ${finding.severity}`}>{finding.severity}</span>
              </td>
              <td>{finding.message}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {findings.length > shown.length && (
        <button
          className="link"
          style={{ marginTop: 12 }}
          onClick={() => setExpanded(true)}
        >
          show the remaining {findings.length - shown.length}
        </button>
      )}
    </section>
  );
}

/* ---------------------------------------------------------------------------------- */
/* Status pill                                                                         */
/* ---------------------------------------------------------------------------------- */

function StatusPill({ health }: { health: Health | null }) {
  if (!health) {
    return (
      <span className="pill">
        <span className="dot" /> checking
      </span>
    );
  }
  if (!health.configured) {
    return (
      <span className="pill">
        <span className="dot bad" /> no service configured
      </span>
    );
  }
  if (!health.ready) {
    return (
      <span className="pill">
        <span className="dot bad" /> service not ready
      </span>
    );
  }
  return (
    <span className="pill">
      <span className="dot ok" /> ready
      {health.model ? ` · ${health.model}` : ""}
      {health.subscription_type ? ` · ${health.subscription_type}` : ""}
    </span>
  );
}
