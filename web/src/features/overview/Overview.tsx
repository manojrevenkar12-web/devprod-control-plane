import { Loaded } from "../../components/States";
import { Strip } from "../../components/Strip";
import { type Rollouts, type Summary, useRollouts, useSummary } from "../../lib/data";
import { compact, num, pct } from "../../lib/format";
import { motionTokens } from "../../theme/motion";

const LANES = [
  { id: "baseline", title: "Baseline", how: "Staging, then every account at once; a person notices and rolls back." },
  { id: "control-plane", title: "Control plane", how: "Dev, staging, a 5% canary and a 25% wave, every tier in each, gated per tier." },
] as const;

function Hero({ rollouts, summary }: { rollouts: Rollouts; summary: Summary }) {
  return (
    <section className="hero" aria-labelledby="hero-title">
      <h1 id="hero-title">Every bad release, and how far it got</h1>
      <p className="lede">
        Each row is one rollout of a release carrying a regression that CI missed. The first two cells are the dev and staging
        accounts; then come the {rollouts.baseline?.accounts.length ?? 0} customer AWS accounts, quietest first. A filled cell
        ran the bad release.
      </p>
      <div className="lanes">
        {LANES.map((lane) => {
          const set = rollouts[lane.id];
          const bad = set?.rollouts.filter((r) => r.truth_bad) ?? [];
          const d = summary.worlds[lane.id].truth.deploys;
          return (
            <article key={lane.id} className={`lane lane--${lane.id}`} aria-label={lane.title}>
              <header className="lane__head">
                <h2>{lane.title}</h2>
                <p>{lane.how}</p>
              </header>
              <p className="lane__figure">
                <strong>{num(d.customer_accounts_hit)}</strong> accounts ran a bad release
              </p>
              <div className="lane__strips">
                {bad.map((r, i) => (
                  <Strip key={r.release} strip={r.strip} internal={r.internal} bad label={`Release ${r.release}`} animate delay={i * motionTokens.stagger} />
                ))}
              </div>
              <p className="lane__foot">
                {d.bad_rollouts} bad rollouts: {d.stopped_by_gate} stopped by a gate, {d.rolled_back_by_person} rolled back by a
                person. {compact(d.bad_decisions)} extra failed decisions.
              </p>
            </article>
          );
        })}
      </div>
    </section>
  );
}

interface Row { label: string; b: number | null; c: number | null; fmt: (x: number | null) => string; better: "lower" | "higher" }

function rows(s: Summary): { group: string; rows: Row[] }[] {
  const b = s.worlds.baseline;
  const c = s.worlds["control-plane"];
  const m = (x: number | null) => num(x, 1);
  const i = (x: number | null) => num(x);
  return [
    { group: "For engineers", rows: [
      { label: "Push to green, p95 (minutes)", b: b.observed.ci.push_to_green_minutes.p95 ?? null, c: c.observed.ci.push_to_green_minutes.p95 ?? null, fmt: m, better: "lower" },
      { label: "Minutes people spent on CI failures", b: b.truth.ci.human_minutes, c: c.truth.ci.human_minutes, fmt: i, better: "lower" },
      { label: "PR lead time, median (hours)", b: b.observed.ci.pr_lead_time_hours.p50, c: c.observed.ci.pr_lead_time_hours.p50, fmt: m, better: "lower" },
    ] },
    { group: "CI", rows: [
      { label: "Regressions that reached main", b: b.truth.ci.regressions_escaped, c: c.truth.ci.regressions_escaped, fmt: i, better: "lower" },
      { label: "Runner minutes billed", b: b.observed.ci.runner_minutes, c: c.observed.ci.runner_minutes, fmt: (x) => (x === null ? "–" : compact(x)), better: "lower" },
      { label: "Job minutes run", b: b.observed.ci.job_minutes, c: c.observed.ci.job_minutes, fmt: (x) => (x === null ? "–" : compact(x)), better: "lower" },
      { label: "Runner queue, p95 (minutes)", b: b.observed.ci.queue_minutes.p95 ?? null, c: c.observed.ci.queue_minutes.p95 ?? null, fmt: m, better: "lower" },
    ] },
    { group: "Delivery", rows: [
      { label: "Change failure rate", b: b.observed.delivery.change_failure_rate, c: c.observed.delivery.change_failure_rate, fmt: (x) => pct(x, 1), better: "lower" },
      { label: "Time to restore, median (minutes)", b: b.observed.delivery.time_to_restore_minutes.p50, c: c.observed.delivery.time_to_restore_minutes.p50, fmt: i, better: "lower" },
      { label: "Lead time for changes, median (hours)", b: b.observed.delivery.lead_time_for_changes_hours.p50, c: c.observed.delivery.lead_time_for_changes_hours.p50, fmt: m, better: "lower" },
      { label: "Good releases rolled back", b: b.truth.deploys.false_rollbacks, c: c.truth.deploys.false_rollbacks, fmt: i, better: "lower" },
    ] },
  ];
}

function verdict(r: Row): "better" | "worse" | "same" {
  if (r.b === null || r.c === null || r.b === r.c) return "same";
  return (r.c < r.b) === (r.better === "lower") ? "better" : "worse";
}

function Compare({ summary }: { summary: Summary }) {
  return (
    <section className="section" aria-labelledby="compare-title">
      <h2 id="compare-title">What changed, and what it cost</h2>
      <p className="note">Ten simulated weeks, the same 1,134 pull requests in both worlds. Rows the control plane makes worse are kept, not hidden.</p>
      <table className="compare">
        <thead>
          <tr><th scope="col">Measure</th><th scope="col">Baseline</th><th scope="col">Control plane</th></tr>
        </thead>
        {rows(summary).map((g) => (
          <tbody key={g.group}>
            <tr className="compare__group"><th scope="rowgroup" colSpan={3}>{g.group}</th></tr>
            {g.rows.map((r) => (
              <tr key={r.label} className={`compare__row compare__row--${verdict(r)}`}>
                <th scope="row">{r.label}</th>
                <td>{r.fmt(r.b)}</td>
                <td><span className="compare__value">{r.fmt(r.c)}</span>{verdict(r) === "worse" && <span className="tag tag--worse">worse</span>}</td>
              </tr>
            ))}
          </tbody>
        ))}
      </table>
    </section>
  );
}

const gnum = (x: number | null) => (x === null ? "–" : num(x, Number.isInteger(x) ? 0 : x < 10 ? 2 : 1));

function Gate({ summary }: { summary: Summary }) {
  const passed = summary.gate.filter((g) => g.passed).length;
  return (
    <section className="section" aria-labelledby="gate-title">
      <h2 id="gate-title">Release gate: {passed} of {summary.gate.length} checks pass</h2>
      <ul className="gate">
        {summary.gate.map((g) => (
          <li key={g.name} className={g.passed ? "gate__item" : "gate__item gate__item--fail"}>
            <span className="gate__mark" aria-hidden="true">{g.passed ? "✓" : "✕"}</span>
            <span className="gate__name">{g.name}</span>
            <span className="gate__value">{gnum(g.value)} {g.op} {gnum(g.bound)}</span>
            <span className="visually-hidden">{g.passed ? "passes" : "fails"}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function Overview() {
  const s = useSummary();
  const r = useRollouts();
  return (
    <>
      <Loaded q={{ ...r, data: r.data && s.data ? { r: r.data, s: s.data } : undefined, isPending: r.isPending || s.isPending, isError: r.isError || s.isError, error: r.error ?? s.error, refetch: () => { r.refetch(); s.refetch(); } }} label="the rollouts">
        {({ r: ro, s: su }) => <Hero rollouts={ro} summary={su} />}
      </Loaded>
      <Loaded q={s} label="the comparison">
        {(su) => (
          <div className="two">
            <Compare summary={su} />
            <Gate summary={su} />
          </div>
        )}
      </Loaded>
    </>
  );
}
