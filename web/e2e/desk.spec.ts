import { expect, test } from "@playwright/test";

test("every view loads under the production CSP without errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Every bad release, and how far it got" })).toBeVisible();
  await expect(page.getByRole("article", { name: "Control plane" }).getByRole("img").first()).toBeVisible();
  for (const [tab, heading] of [["CI", "CI, day by day"], ["Flaky tests", "Flaky tests"], ["Triage", "What the triage agent posted"],
    ["Rollouts", "Rollouts to customer accounts"], ["Held-out", "Held-out failures"]] as const) {
    await page.getByRole("tab", { name: tab }).click();
    await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
  }
  expect(errors).toEqual([]);
});

test("rollouts: switch deploy settings and show every release", async ({ page }) => {
  await page.goto("/#rollouts");
  await page.getByRole("button", { name: "Baseline" }).click();
  const bad = await page.locator(".rollouts__row").count();
  await page.getByLabel("Only releases with a regression").uncheck();
  expect(await page.locator(".rollouts__row").count()).toBeGreaterThan(bad);
  await page.getByRole("button", { name: "Usual canary (ablation)" }).click();
  await expect(page.getByRole("button", { name: "Usual canary (ablation)" })).toHaveAttribute("aria-pressed", "true");
});

test("triage: open a comment and read the log it was based on", async ({ page }) => {
  await page.goto("/#triage");
  const first = page.locator(".feed__head").first();
  await first.click();
  await expect(first).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByLabel("End of the failed job's log")).toBeVisible();
});

test("a missing data file shows an error with a way to retry", async ({ page }) => {
  await page.route("**/data/flaky.json", (r) => r.fulfill({ status: 404, body: "" }));
  await page.goto("/#flaky");
  await expect(page.getByRole("alert")).toContainText("devprod export-web");
  await expect(page.getByRole("button", { name: "Load again" })).toBeVisible();
});

test("dark mode keeps the hero readable", async ({ browser }) => {
  const page = await browser.newPage({ colorScheme: "dark" });
  await page.goto("/");
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  expect(bg).toBe("rgb(15, 23, 30)");
  await page.close();
});
