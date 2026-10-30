import { useQuery } from "@tanstack/react-query";
import { setStart } from "./format";

/** The files `devprod export-web` writes to public/data. Fields named truth_* come from the
 *  simulation's truth (what really happened), the rest from what CI, GitHub and AWS recorded. */
export type Policy = "baseline" | "control-plane";
export const POLICIES: Policy[] = ["baseline", "control-plane"];

export interface Quantiles { p50: number | null; p90?: number | null; p95?: number | null; mean?: number | null; max?: number | null }

export interface ObservedCi {
  prs: number; merged: number; runs: number; jobs: number; job_minutes: number; runner_minutes: number; peak_runners: number;
  pr_lead_time_hours: Quantiles; ci_wall_minutes: Quantiles; push_to_green_minutes: Quantiles; queue_minutes: Quantiles;
  failed_push_share: number; failed_then_passed_same_commit: number; cache_hit_share: number; prs_per_engineer_day: number;
  quarantined_tests: number; triage_comments: number; engineer_minutes_on_failures: number;
}
export interface ObservedDelivery {
  releases: number; rollouts_reaching_customers: number; completed_rollouts: number; deploys_per_working_day: number;
  change_failure_rate: number; lead_time_for_changes_hours: Quantiles; time_to_restore_minutes: Quantiles; rollbacks: number;
}
export interface Agent { judged: number; accuracy: number; regression_precision: number; regression_recall: number; confusion: Record<string, Record<string, number>> }
export interface TruthCi {
  regressions: number; regressions_caught: number; regressions_escaped: number; regressions_hidden_by_quarantine: number;
  human_minutes: number; failure_incidents: Record<string, number>; agent?: Agent;
  flake_detector: { quarantined: number; truly_flaky: number; truly_flaky_seen_failing: number; precision: number | null; recall_of_seen_failing: number | null; recall_of_flake_rate_ge_2pct: number | null; wrongly_quarantined: string[] };
}
export interface Deploys {
  releases: number; rollouts: number; completed: number; bad_releases: number; bad_rollouts: number; stopped_by_gate: number;
  false_rollbacks: number; rolled_back_by_person: number; customer_accounts_hit: number; bad_decisions: number;
  exposure_decision_minutes: number; release_to_95pct_minutes_median: number | null;
}
export interface World { observed: { ci: ObservedCi; delivery: ObservedDelivery }; truth: { ci: TruthCi; deploys: Deploys }; summary: { agent_usd: number; runner_minutes: number } }
export interface GateCheck { name: string; passed: boolean; value: number | null; op: string; bound: number | null }
export interface Summary { start: string; worlds: Record<Policy, World>; ablations: Record<string, Deploys>; gate: GateCheck[] }

export interface Day { day: number; weekday: number; runs: number; failed: number; wall_p50: number | null; runner_minutes: number; peak_runners: number }
export type Daily = Record<Policy, Day[]>;

export interface QuarantineRow { test_id: string; owner: string; since_ts: number; until_ts: number; reason: string; status: string; clean_runs: number; truth_flake_rate: number }
export interface Suspect { test_id: string; runs: number; fails: number; flips: number; p_flaky: number; truth_flake_rate: number; quarantined: boolean }
export interface Flaky { quarantine: QuarantineRow[]; suspects: Suspect[]; truly_flaky: number }

export interface TriageItem { run_id: number; pr: number; ts: number; classification: string; body: string; attempt: number; trigger: string; module: string | null; log_tail: string; truth_kind: string | null; final_verdict: string | null }

export interface RolloutRow { release: number; state: string; truth_bad: boolean; internal: string[]; customers_reached: number; strip: string; stopped_at: string | null; started: number | null }
export interface RolloutSet { policy: Policy; variant: string | null; accounts: { tier: string; traffic: number; window: string }[]; rollouts: RolloutRow[] }
export type Rollouts = Record<string, RolloutSet>;

export interface HeldoutMiss { id: string; expected: string; got: string; expected_next: string; got_next: string; why: string; evidence: string[] }
export interface Heldout { set: string; status: string; cases: number; kind_accuracy: number; next_step_accuracy: number; by_kind: Record<string, { cases: number; right: number }>; confusion: Record<string, Record<string, number>>; authors_sent_chasing_noise: number; misses: HeldoutMiss[] }

async function load<T>(path: string): Promise<T> {
  const res = await fetch(`./data/${path}`);
  if (!res.ok) throw new Error(`Could not load ${path} (HTTP ${res.status}). Run \`devprod run && devprod export-web\`, then reload.`);
  return (await res.json()) as T;
}

const loadSummary = async () => {
  const s = await load<Summary>("summary.json");
  setStart(s.start);
  return s;
};
export const useSummary = () => useQuery({ queryKey: ["summary"], queryFn: loadSummary });
/** Views that show dates load the (cached) summary first, for the simulation's start date. */
const withStart = async <T,>(path: string): Promise<T> => {
  await loadSummary().catch(() => undefined);
  return load<T>(path);
};
export const useDaily = () => useQuery({ queryKey: ["daily"], queryFn: () => withStart<Daily>("daily.json") });
export const useFlaky = () => useQuery({ queryKey: ["flaky"], queryFn: () => withStart<Flaky>("flaky.json") });
export const useTriage = () => useQuery({ queryKey: ["triage"], queryFn: () => withStart<TriageItem[]>("triage.json") });
export const useRollouts = () => useQuery({ queryKey: ["rollouts"], queryFn: () => withStart<Rollouts>("rollouts.json") });
export const useHeldout = () => useQuery({ queryKey: ["heldout"], queryFn: () => load<Heldout[]>("heldout.json") });
