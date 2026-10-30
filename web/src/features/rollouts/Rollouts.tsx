import { useState } from "react";
import { EmptyState, Loaded } from "../../components/States";
import { Strip } from "../../components/Strip";
import { type RolloutRow, type Rollouts as RolloutsData, useRollouts } from "../../lib/data";
import { simTime } from "../../lib/format";

const SETS: { id: string; label: string }[] = [
  { id: "baseline", label: "Baseline" },
  { id: "control-plane", label: "Control plane" },
  { id: "control-plane-pooled", label: "Usual canary (ablation)" },
];

const stateText = (r: RolloutRow) =>
  r.state === "completed" ? "reached everyone" : r.stopped_at ? `stopped at ${r.stopped_at}` : r.state === "rolled_back" ? "rolled back by a person" : r.state;

function List({ data }: { data: RolloutsData }) {
  const [set, setSet] = useState("control-plane");
  const [onlyBad, setOnlyBad] = useState(true);
  const rows = (data[set]?.rollouts ?? []).filter((r) => !onlyBad || r.truth_bad);
  return (
    <>
      <div className="controls">
        <div className="segmented" role="group" aria-label="Deploy settings">
          {SETS.filter((s) => data[s.id]).map((s) => (
            <button key={s.id} type="button" aria-pressed={set === s.id} onClick={() => setSet(s.id)}>{s.label}</button>
          ))}
        </div>
        <label className="check">
          <input type="checkbox" checked={onlyBad} onChange={(e) => setOnlyBad(e.target.checked)} /> Only releases with a regression
        </label>
      </div>
      {rows.length === 0 ? (
        <EmptyState title="No rollouts to show">Clear the filter to see every rollout.</EmptyState>
      ) : (
        <ol className="rollouts">
          {rows.map((r) => (
            <li key={r.release} className="rollouts__row">
              <span className="rollouts__id">Release {r.release}</span>
              <Strip strip={r.strip} internal={r.internal} bad={r.truth_bad} label={`Release ${r.release}`} />
              <span className={`rollouts__state rollouts__state--${r.state}`}>{stateText(r)}</span>
              <span className="rollouts__when">{r.started !== null ? simTime(r.started) : ""}</span>
            </li>
          ))}
        </ol>
      )}
    </>
  );
}

export function Rollouts() {
  const q = useRollouts();
  return (
    <section className="section" aria-labelledby="ro-title">
      <h1 id="ro-title">Rollouts to customer accounts</h1>
      <p className="lede">The first two cells are dev and staging, then the customer accounts by traffic, quietest on the left. Customers who only take deploys at night are reached when their window opens.</p>
      <Loaded q={q} label="rollouts">{(d) => <List data={d} />}</Loaded>
    </section>
  );
}
