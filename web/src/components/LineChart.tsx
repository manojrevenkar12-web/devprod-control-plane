/** A small two-series line chart over working days. No library: an SVG with a baseline, a few
 *  ticks and direct labels at the line ends instead of a legend. */
export interface Series { name: string; tone: "cp" | "base"; points: (number | null)[] }

const W = 520;
const H = 160;
const PAD = { l: 44, r: 96, t: 12, b: 24 };

export function LineChart({ series, labels, unit, title }: { series: Series[]; labels: string[]; unit: string; title: string }) {
  const values = series.flatMap((s) => s.points.filter((v): v is number => v !== null));
  const max = Math.max(1, ...values) * 1.08;
  const n = labels.length;
  const x = (i: number) => PAD.l + (n <= 1 ? 0 : (i / (n - 1)) * (W - PAD.l - PAD.r));
  const y = (v: number) => PAD.t + (1 - v / max) * (H - PAD.t - PAD.b);
  const ticks = [0, max / 2, max].map((v) => Math.round(v));
  const path = (pts: (number | null)[]) =>
    pts.map((v, i) => (v === null ? "" : `${i === 0 || pts[i - 1] === null ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`)).join("");
  return (
    <figure className="chart">
      <figcaption>{title}</figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}. ${series.map((s) => `${s.name}: last ${s.points.at(-1) ?? "–"} ${unit}`).join("; ")}`}>
        {ticks.map((t) => (
          <g key={t} className="chart__tick">
            <line x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} />
            <text x={PAD.l - 6} y={y(t)} dy="0.32em" textAnchor="end">{t.toLocaleString("en-GB")}</text>
          </g>
        ))}
        {[0, Math.floor((n - 1) / 2), n - 1].map((i) => (
          <text key={i} className="chart__x" x={x(i)} y={H - 6} textAnchor="middle">{labels[i]}</text>
        ))}
        {series.map((s) => {
          const lastIdx = s.points.findLastIndex((v) => v !== null);
          const last = s.points[lastIdx];
          return (
            <g key={s.name} className={`chart__series chart__series--${s.tone}`}>
              <path d={path(s.points)} />
              {last !== null && last !== undefined && (
                <text x={x(lastIdx) + 8} y={y(last)} dy="0.32em">{s.name}</text>
              )}
            </g>
          );
        })}
      </svg>
    </figure>
  );
}
