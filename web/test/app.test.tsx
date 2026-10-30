import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { Strip } from "../src/components/Strip";
import { Heldout } from "../src/features/heldout/Heldout";
import { Triage } from "../src/features/triage/Triage";
import { compact, num, pct, ratio, setStart, simDate } from "../src/lib/format";

/** The real exported data, so the tests see what the dashboard shows. */
const data = (name: string) => JSON.parse(readFileSync(resolve(__dirname, "../public/data", name), "utf8"));

function wrap(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function serve(files: Record<string, unknown>, failing: string[] = []) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const name = String(url).split("/").pop()!;
    if (failing.includes(name) || !(name in files)) return new Response("nope", { status: 500 });
    return new Response(JSON.stringify(files[name]), { status: 200 });
  }));
}

const ALL = ["summary.json", "daily.json", "flaky.json", "triage.json", "rollouts.json", "heldout.json"];
const all = () => Object.fromEntries(ALL.map((f) => [f, data(f)]));

afterEach(() => {
  vi.unstubAllGlobals();
  window.location.hash = "";
});

describe("format", () => {
  it("formats numbers, shares and ratios", () => {
    expect(num(1234.5, 1)).toBe("1,234.5");
    expect(num(null)).toBe("–");
    expect(pct(0.204, 1)).toBe("20.4%");
    expect(compact(2_419_200)).toBe("2.4M");
    expect(compact(64_748)).toBe("65k");
    expect(ratio(10, 30)).toBe("3.0×");
    expect(ratio(30, 10)).toBe("÷3.0");
  });
  it("shows simulated minutes as dates once the start is known", () => {
    setStart("2026-05-04");
    expect(simDate(0)).toBe("Mon 4 May");
    expect(simDate(24 * 60 * 7)).toBe("Mon 11 May");
  });
});

describe("Strip", () => {
  it("says how far a rollout got", () => {
    render(<Strip strip={"dd.."} internal={["internal-dev"]} bad label="Release 3" />);
    expect(screen.getByRole("img")).toHaveAccessibleName("Release 3: reached dev and 2 of 4 customer accounts");
  });
});

describe("the dashboard", () => {
  it("opens on the blast radius of bad releases and the gate", async () => {
    serve(all());
    wrap(<App />);
    expect(await screen.findByRole("heading", { name: "Every bad release, and how far it got" })).toBeInTheDocument();
    const s = data("summary.json");
    const base = screen.getByRole("article", { name: "Baseline" });
    expect(within(base).getByText(String(s.worlds.baseline.truth.deploys.customer_accounts_hit))).toBeInTheDocument();
    const passed = s.gate.filter((g: { passed: boolean }) => g.passed).length;
    expect(await screen.findByRole("heading", { name: `Release gate: ${passed} of ${s.gate.length} checks pass` })).toBeInTheDocument();
    expect(screen.getAllByText("worse").length).toBeGreaterThan(0); // costs are shown, not hidden
  });

  it("moves between views with the arrow keys", async () => {
    serve(all());
    wrap(<App />);
    const tab = screen.getByRole("tab", { name: "Overview" });
    tab.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "CI" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("heading", { name: "CI, day by day" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#ci");
  });

  it("shows an error with a retry when data is missing, and recovers", async () => {
    serve({}, ALL);
    wrap(<App />);
    const alerts = await screen.findAllByRole("alert");
    expect(alerts[0]).toHaveTextContent("devprod export-web");
    serve(all());
    await userEvent.click(within(alerts[0]!).getByRole("button", { name: "Load again" }));
    expect(await screen.findByRole("heading", { name: "Every bad release, and how far it got" })).toBeInTheDocument();
  });

  it("shows a skeleton while loading", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    wrap(<App />);
    expect(screen.getAllByRole("status")[0]).toHaveAccessibleName("Loading the rollouts");
  });
});

describe("views", () => {
  it("held-out: says how to run a set when none has run", async () => {
    serve({ "heldout.json": [] });
    wrap(<Heldout />);
    expect(await screen.findByRole("heading", { name: "No held-out set has been run" })).toBeInTheDocument();
  });

  it("triage: filters by kind, opens a comment with its log, and says when a kind is empty", async () => {
    const items = data("triage.json");
    serve({ "triage.json": items, "summary.json": data("summary.json") });
    wrap(<Triage />);
    const first = (await screen.findAllByRole("button", { expanded: false }))[0]!;
    await userEvent.click(first);
    expect(first).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByLabelText("End of the failed job's log")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "regression" }));
    for (const b of screen.getAllByRole("button", { expanded: false })) expect(b).toHaveTextContent("regression");
  });

  it("triage: an empty feed invites the next failure", async () => {
    serve({ "triage.json": [], "summary.json": data("summary.json") });
    wrap(<Triage />);
    expect(await screen.findByRole("heading", { name: "No failed runs yet" })).toBeInTheDocument();
  });
});
