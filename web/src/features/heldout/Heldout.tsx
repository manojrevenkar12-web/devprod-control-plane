import { EmptyState, Loaded } from "../../components/States";
import { type Heldout as HeldoutData, useHeldout } from "../../lib/data";
import { pct } from "../../lib/format";

function Set({ h }: { h: HeldoutData }) {
  return (
    <article className="section" aria-labelledby={`h-${h.set}`}>
      <h2 id={`h-${h.set}`}>{h.set}</h2>
      <p className="note">
        {h.cases} failures written blind by someone who had not seen the agent, scored on the first run (recorded once, never
        rewritten). Kind right {pct(h.kind_accuracy)}, next step right {pct(h.next_step_accuracy)}; authors told a flake was their
        fault: {h.authors_sent_chasing_noise}. Status: {h.status}.
      </p>
      <table className="table table--narrow">
        <thead><tr><th scope="col">Really</th><th scope="col">Cases</th><th scope="col">Right</th></tr></thead>
        <tbody>
          {Object.entries(h.by_kind).map(([k, v]) => (
            <tr key={k}><th scope="row">{k}</th><td>{v.cases}</td><td>{v.right}</td></tr>
          ))}
        </tbody>
      </table>
      {h.misses.length > 0 && (
        <>
          <h3>What it got wrong</h3>
          <ul className="misses">
            {h.misses.map((m) => (
              <li key={m.id}>
                <code>{m.id}</code>: said <strong>{m.got}</strong>, really {m.expected}. {m.why}
              </li>
            ))}
          </ul>
        </>
      )}
    </article>
  );
}

export function Heldout() {
  const q = useHeldout();
  return (
    <>
      <h1 className="page-title">Held-out failures</h1>
      <Loaded q={q} label="held-out results" empty={(d) => (d.length === 0 ? (
        <EmptyState title="No held-out set has been run">Run <code>devprod heldout ci-failures-1</code> once, then <code>devprod export-web</code>.</EmptyState>
      ) : null)}>
        {(d) => <>{d.map((h) => <Set key={h.set} h={h} />)}</>}
      </Loaded>
    </>
  );
}
