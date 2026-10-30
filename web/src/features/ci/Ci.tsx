import { LineChart } from "../../components/LineChart";
import { Loaded } from "../../components/States";
import { type Daily, type Summary, useDaily, useSummary } from "../../lib/data";
import { num, pct, simDate } from "../../lib/format";

const DAY = 24 * 60;

function Charts({ daily }: { daily: Daily }) {
  const work = (d: Daily["baseline"]) => d.filter((x) => x.weekday < 5);
  const b = work(daily.baseline);
  const c = work(daily["control-plane"]);
  const labels = b.map((x) => simDate(x.day * DAY));
  const pair = (f: (x: Daily["baseline"][number]) => number | null) => [
    { name: "baseline", tone: "base" as const, points: b.map(f) },
    { name: "control plane", tone: "cp" as const, points: c.map(f) },
  ];
  return (
    <div className="charts">
      <LineChart title="Failed CI runs per working day" unit="runs" labels={labels} series={pair((x) => x.failed)} />
      <LineChart title="CI wall time of a push, median (minutes)" unit="minutes" labels={labels} series={pair((x) => x.wall_p50)} />
      <LineChart title="Runner hours billed per day" unit="hours" labels={labels} series={pair((x) => Math.round(x.runner_minutes / 60))} />
      <LineChart title="Most runners up at once" unit="runners" labels={labels} series={pair((x) => x.peak_runners)} />
    </div>
  );
}

const KINDS = ["infra", "dependency", "flaky", "regression"];

function Agent({ summary }: { summary: Summary }) {
  const a = summary.worlds["control-plane"].truth.ci.agent;
  const fd = summary.worlds["control-plane"].truth.ci.flake_detector;
  if (!a) return null;
  return (
    <div className="two">
      <section className="section" aria-labelledby="agent-title">
        <h2 id="agent-title">The triage agent, against what really failed</h2>
        <p className="note">
          {num(a.judged)} failures judged: right {pct(a.accuracy, 1)} of the time. When it blamed the change it was right{" "}
          {pct(a.regression_precision, 1)} of the time, and it found {pct(a.regression_recall, 0)} of the real regressions.
        </p>
        <table className="matrix">
          <caption className="visually-hidden">Rows: what failed. Columns: what the agent said.</caption>
          <thead><tr><th scope="col">Really</th>{KINDS.map((k) => <th key={k} scope="col">{k}</th>)}</tr></thead>
          <tbody>
            {KINDS.map((k) => (
              <tr key={k}>
                <th scope="row">{k}</th>
                {KINDS.map((g) => {
                  const v = a.confusion[k]?.[g] ?? 0;
                  return <td key={g} className={v === 0 ? "matrix__zero" : g === k ? "matrix__hit" : "matrix__miss"}>{v}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section className="section" aria-labelledby="fd-title">
        <h2 id="fd-title">Flaky-test detection</h2>
        <p className="note">
          {fd.quarantined} tests quarantined, {fd.wrongly_quarantined.length === 0 ? "none of them wrongly" : `${fd.wrongly_quarantined.length} wrongly`}.
          Of the {fd.truly_flaky_seen_failing} truly flaky tests that failed at least once, it found {pct(fd.recall_of_seen_failing)};
          the rest flipped too rarely in ten weeks to tell from noise. Quarantine never hid a regression.
        </p>
      </section>
    </div>
  );
}

export function Ci() {
  const d = useDaily();
  const s = useSummary();
  return (
    <>
      <section className="section" aria-labelledby="ci-title">
        <h1 id="ci-title">CI, day by day</h1>
        <p className="lede">Affected-module selection runs more jobs, shards them so each finishes sooner, and an autoscaled fleet bills for the hours it is busy rather than for 24 runners around the clock.</p>
        <p className="note">More jobs also means more failed runs: more chances for a runner to die or a flaky test to flip. Most of those are rerun by the agent without anyone looking.</p>
        <Loaded q={d} label="daily CI" empty={(x) => (x.baseline.length === 0 ? <p>No CI runs recorded yet.</p> : null)}>
          {(x) => <Charts daily={x} />}
        </Loaded>
      </section>
      <Loaded q={s} label="the triage results">{(x) => <Agent summary={x} />}</Loaded>
    </>
  );
}
