const DAY = 24 * 60;
const nf = new Intl.NumberFormat("en-GB");

export const num = (x: number | null | undefined, digits = 0): string =>
  x === null || x === undefined || Number.isNaN(x) ? "–" : new Intl.NumberFormat("en-GB", { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(x);

export const pct = (x: number | null | undefined, digits = 0): string => (x === null || x === undefined ? "–" : `${num(x * 100, digits)}%`);

export const compact = (x: number): string => (Math.abs(x) >= 1e6 ? `${num(x / 1e6, 1)}M` : Math.abs(x) >= 1e4 ? `${num(x / 1e3, 0)}k` : nf.format(Math.round(x)));

/** The simulation counts minutes from local midnight of its first day (`start` in config/org.yaml,
 *  exported with the data). Shown as Berlin wall-clock time, which is what the minutes mean. */
let START = Number.NaN;
export const setStart = (iso: string) => {
  START = Date.parse(`${iso}T00:00:00Z`);
};
const at = (minutes: number) => new Date(START + minutes * 60_000);
export const simDate = (minutes: number): string =>
  Number.isNaN(START) ? `day ${Math.floor(minutes / DAY) + 1}`
    : at(minutes).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
export const simTime = (minutes: number): string =>
  Number.isNaN(START) ? simDate(minutes)
    : `${simDate(minutes)} ${at(minutes).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" })}`;
export const days = (minutes: number): string => `${num(minutes / DAY, 0)} days`;

/** "3.2 → 0.6": how a measure moved from the baseline to the control plane. */
export const ratio = (before: number, after: number): string => {
  if (!before) return "–";
  const r = after / before;
  return r >= 1 ? `${num(r, 1)}×` : `÷${num(1 / r, r < 0.1 ? 0 : 1)}`;
};
