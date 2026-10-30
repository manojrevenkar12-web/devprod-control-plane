import { expect, test } from "@playwright/test";

test("phone: no sideways scroll and the lanes stack", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Every bad release, and how far it got" })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  const [a, b] = await Promise.all([
    page.getByRole("article", { name: "Baseline" }).boundingBox(),
    page.getByRole("article", { name: "Control plane" }).boundingBox(),
  ]);
  expect(b!.y).toBeGreaterThan(a!.y + a!.height - 1);
});
