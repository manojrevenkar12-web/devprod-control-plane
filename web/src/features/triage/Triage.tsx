import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useMemo, useState } from "react";
import { EmptyState, Loaded } from "../../components/States";
import { type TriageItem, useTriage } from "../../lib/data";
import { simTime } from "../../lib/format";
import { motionTokens } from "../../theme/motion";

const FILTERS = ["all", "infra", "dependency", "flaky", "regression"] as const;
type Filter = (typeof FILTERS)[number];

function Body({ text }: { text: string }) {
  return (
    <div className="comment">
      {text.split("\n").map((line, i) => (
        <p key={i}>{line.replace(/\*\*/g, "").replace(/`/g, "")}</p>
      ))}
    </div>
  );
}

function Item({ item }: { item: TriageItem }) {
  const [open, setOpen] = useState(false);
  const reduce = useReducedMotion();
  const truthDiffers = item.truth_kind && item.final_verdict && item.final_verdict !== item.truth_kind;
  return (
    <li className="feed__item">
      <button type="button" className="feed__head" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span className={`kind kind--${item.classification}`}>{item.classification}</span>
        <span className="feed__what">
          PR #{item.pr}, run {item.run_id}{item.module ? ` in ${item.module}` : ""}
          {item.trigger !== "push" ? ` (rerun, attempt ${item.attempt})` : ""}
        </span>
        <span className="feed__when">{simTime(item.ts)}</span>
        {item.truth_kind && (
          <span className={truthDiffers ? "tag tag--worse" : "tag tag--ok"}>
            {truthDiffers ? `really ${item.truth_kind}` : "right"}
          </span>
        )}
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div className="feed__detail" initial={reduce ? false : { height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }} exit={reduce ? undefined : { height: 0, opacity: 0 }}
            transition={{ duration: motionTokens.base, ease: motionTokens.ease }}>
            <Body text={item.body} />
            {item.log_tail && <pre className="log" aria-label="End of the failed job's log">{item.log_tail}</pre>}
          </motion.div>
        )}
      </AnimatePresence>
    </li>
  );
}

function Feed({ items }: { items: TriageItem[] }) {
  const [filter, setFilter] = useState<Filter>("all");
  const shown = useMemo(() => items.filter((i) => filter === "all" || i.classification === filter), [items, filter]);
  return (
    <>
      <div className="segmented" role="group" aria-label="Show">
        {FILTERS.map((f) => (
          <button key={f} type="button" aria-pressed={filter === f} onClick={() => setFilter(f)}>
            {f === "all" ? "Everything" : f}
          </button>
        ))}
      </div>
      {shown.length === 0 ? (
        <EmptyState title={`No ${filter} failures in this window`}>Pick another kind to see its comments.</EmptyState>
      ) : (
        <ul className="feed">{shown.map((i) => <Item key={`${i.run_id}-${i.ts}`} item={i} />)}</ul>
      )}
    </>
  );
}

export function Triage() {
  const q = useTriage();
  return (
    <section className="section" aria-labelledby="triage-title">
      <h1 id="triage-title">What the triage agent posted</h1>
      <p className="lede">The latest comments on failed runs. A suspected regression is rerun once before the author is told; open a comment to see the evidence and the end of the log it read.</p>
      <Loaded q={q} label="triage comments" empty={(d) => (d.length === 0 ? <EmptyState title="No failed runs yet">When a run fails, the agent's diagnosis appears here.</EmptyState> : null)}>
        {(d) => <Feed items={d} />}
      </Loaded>
    </section>
  );
}
