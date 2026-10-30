import { motion } from "motion/react";
import { type KeyboardEvent, useRef } from "react";
import { motionTokens } from "../theme/motion";

export interface Tab { id: string; label: string }

/** WAI-ARIA tabs: arrow keys move between tabs, the panel is labelled by its tab. */
export function Tabs({ tabs, current, onChange }: { tabs: Tab[]; current: string; onChange: (id: string) => void }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: KeyboardEvent, i: number) => {
    const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    const next = (i + d + tabs.length) % tabs.length;
    refs.current[next]?.focus();
    onChange(tabs[next]!.id);
  };
  return (
    <div className="tabs" role="tablist" aria-label="Views">
      {tabs.map((t, i) => (
        <button key={t.id} ref={(el) => { refs.current[i] = el; }} type="button" role="tab" id={`tab-${t.id}`}
          aria-selected={current === t.id} aria-controls={`panel-${t.id}`} tabIndex={current === t.id ? 0 : -1}
          className="tabs__tab" onClick={() => onChange(t.id)} onKeyDown={(e) => onKey(e, i)}>
          {t.label}
          {current === t.id && (
            <motion.span className="tabs__mark" layoutId="tab-mark" transition={{ duration: motionTokens.base, ease: motionTokens.ease }} />
          )}
        </button>
      ))}
    </div>
  );
}
