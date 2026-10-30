import { motion, useReducedMotion } from "motion/react";
import { useId } from "react";
import { motionTokens } from "../theme/motion";

/** One rollout as a row of accounts: dev and staging, then the customers, quietest first. A filled
 *  cell ran the release. */
const CELL = 4;
const GAP = 1;
const HEIGHT = 14;

const INTERNAL = 2; // dev and staging come first, set apart by a wider gap
const SPLIT = 3 * CELL;

export const stripWidth = (n: number) => INTERNAL * (CELL + GAP) + SPLIT + n * (CELL + GAP) - GAP;

export function Strip({ strip, internal = [], bad, label, delay = 0, animate = false }: {
  strip: string; internal?: string[]; bad: boolean; label: string; delay?: number; animate?: boolean;
}) {
  const reduce = useReducedMotion();
  const clip = useId();
  const w = stripWidth(strip.length);
  const reached = [...strip].filter((c) => c !== ".").length;
  const tone = bad ? "fail" : "ok";
  const offset = INTERNAL * (CELL + GAP) + SPLIT;
  const envs = ["internal-dev", "internal-staging"];
  return (
    <svg className="strip" viewBox={`0 0 ${w} ${HEIGHT}`} preserveAspectRatio="none" role="img"
      aria-label={`${label}: reached ${internal.length ? `${internal.map((e) => e.replace("internal-", "")).join(" and ")} and ` : ""}${reached} of ${strip.length} customer accounts`}>
      <defs>
        <clipPath id={clip}>
          {animate && !reduce ? (
            <motion.rect x={0} y={0} height={HEIGHT} initial={{ width: 0 }} animate={{ width: w }}
              transition={{ duration: motionTokens.slow, delay, ease: motionTokens.ease }} />
          ) : (
            <rect x={0} y={0} width={w} height={HEIGHT} />
          )}
        </clipPath>
      </defs>
      {envs.map((e, i) => (
        <rect key={e} className={internal.includes(e) ? "strip__env strip__env--on" : "strip__env"} x={i * (CELL + GAP)} y={0}
          width={CELL} height={HEIGHT} />
      ))}
      <rect className="strip__bed" x={offset} y={0} width={w - offset} height={HEIGHT} />
      <g clipPath={`url(#${clip})`}>
        {[...strip].map((c, i) =>
          c === "." ? null : (
            <rect key={i} x={offset + i * (CELL + GAP)} y={0} width={CELL} height={HEIGHT} className={`strip__cell strip__cell--${tone}`} />
          ),
        )}
      </g>
    </svg>
  );
}
