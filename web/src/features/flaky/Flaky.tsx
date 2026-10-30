import { EmptyState, Loaded } from "../../components/States";
import { type Flaky as FlakyData, useFlaky } from "../../lib/data";
import { num, pct, simDate } from "../../lib/format";

function Tables({ data }: { data: FlakyData }) {
  return (
    <>
      <section className="section" aria-labelledby="q-title">
        <h2 id="q-title">Quarantined: running, not blocking merges</h2>
        <p className="note">A test is quarantined after at least two flips, when its flip rate is above 1% with 90% probability. Its owning team has ten days to fix it; it gates again after 60 clean runs.</p>
        <div className="scroll">
          <table className="table">
            <thead><tr><th scope="col">Test</th><th scope="col">Owner</th><th scope="col">Since</th><th scope="col">Fix by</th><th scope="col">Evidence</th><th scope="col">Status</th><th scope="col">True flake rate</th></tr></thead>
            <tbody>
              {data.quarantine.map((q) => (
                <tr key={q.test_id}>
                  <th scope="row"><code>{q.test_id}</code></th>
                  <td>{q.owner}</td>
                  <td>{simDate(q.since_ts)}</td>
                  <td>{simDate(q.until_ts)}</td>
                  <td>{q.reason}</td>
                  <td><span className={`tag tag--${q.status === "graduated" ? "ok" : "flaky"}`}>{q.status}</span></td>
                  <td>{pct(q.truth_flake_rate, 1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="section" aria-labelledby="s-title">
        <h2 id="s-title">Most flips</h2>
        <p className="note">The true flake rate is the simulation's; the detector only sees runs, failures and flips.</p>
        <div className="scroll">
          <table className="table">
            <thead><tr><th scope="col">Test</th><th scope="col">Runs</th><th scope="col">Failures</th><th scope="col">Flips</th><th scope="col">P(flaky)</th><th scope="col">Quarantined</th><th scope="col">True flake rate</th></tr></thead>
            <tbody>
              {data.suspects.map((t) => (
                <tr key={t.test_id}>
                  <th scope="row"><code>{t.test_id}</code></th>
                  <td>{num(t.runs)}</td><td>{num(t.fails)}</td><td>{num(t.flips)}</td><td>{num(t.p_flaky, 2)}</td>
                  <td>{t.quarantined ? "yes" : "no"}</td>
                  <td>{pct(t.truth_flake_rate, 1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}

export function Flaky() {
  const q = useFlaky();
  return (
    <>
      <h1 className="page-title">Flaky tests</h1>
      <Loaded q={q} label="flaky tests" empty={(d) => (d.quarantine.length === 0 && d.suspects.length === 0
        ? <EmptyState title="No flips recorded">No test has failed and then passed on the same commit yet. Quarantine starts with the first repeated flip.</EmptyState>
        : null)}>
        {(d) => <Tables data={d} />}
      </Loaded>
    </>
  );
}
