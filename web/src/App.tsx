import type React from "react";
import { useEffect, useState } from "react";
import { type Tab, Tabs } from "./components/Tabs";
import { Ci } from "./features/ci/Ci";
import { Flaky } from "./features/flaky/Flaky";
import { Heldout } from "./features/heldout/Heldout";
import { Overview } from "./features/overview/Overview";
import { Rollouts } from "./features/rollouts/Rollouts";
import { Triage } from "./features/triage/Triage";

const TABS: Tab[] = [
  { id: "overview", label: "Overview" },
  { id: "ci", label: "CI" },
  { id: "flaky", label: "Flaky tests" },
  { id: "triage", label: "Triage" },
  { id: "rollouts", label: "Rollouts" },
  { id: "heldout", label: "Held-out" },
];

const VIEWS: Record<string, () => React.JSX.Element> = { overview: Overview, ci: Ci, flaky: Flaky, triage: Triage, rollouts: Rollouts, heldout: Heldout };

const fromHash = () => {
  const h = window.location.hash.replace("#", "");
  return TABS.some((t) => t.id === h) ? h : "overview";
};

export function App() {
  const [tab, setTab] = useState(fromHash);
  useEffect(() => {
    const on = () => setTab(fromHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const go = (id: string) => {
    setTab(id);
    window.history.replaceState(null, "", `#${id}`);
  };
  const View = VIEWS[tab] ?? Overview;
  return (
    <div className="app">
      <a className="skip" href="#main">Skip to content</a>
      <header className="masthead">
        <p className="brand">devprod <span>control plane</span></p>
        <p className="masthead__scope">A simulated 42-engineer monorepo and 260 customer AWS accounts, ten weeks, before and after.</p>
      </header>
      <nav aria-label="Views"><Tabs tabs={TABS} current={tab} onChange={go} /></nav>
      <main id="main" role="tabpanel" aria-labelledby={`tab-${tab}`} tabIndex={-1}>
        <View />
      </main>
      <footer className="footer">
        <p>Numbers come from <code>devprod run</code>; the simulation's truth is used only to score the platform, never by it.</p>
      </footer>
    </div>
  );
}
