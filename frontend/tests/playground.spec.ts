import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { API, collection, feature, mockApi } from "./helpers";

const RESULTS = collection([feature("Eiffel Tower, Paris", 2.2945, 48.8584, "a"), feature("Tour Eiffel 2", 2.3, 48.85, "b")]);

test.describe("with a mocked API", () => {
  test("starts with instructions, and examples fill the form without searching", async ({ page }) => {
    const seen = await mockApi(page, () => ({ body: RESULTS }));
    await page.goto("/");
    await expect(page.getByRole("status")).toHaveText("Enter a place or address to begin.");
    await page.getByRole("button", { name: "Eiffel Tower, Paris" }).click();
    await expect(page.getByLabel("Place or address")).toHaveValue("Eiffel Tower, Paris");
    await page.waitForTimeout(300);
    expect(seen).toEqual([]);
  });

  test("search shows labels, coordinates, status and request time", async ({ page }) => {
    const seen = await mockApi(page, () => ({ body: RESULTS, headers: { "X-Cache": "MISS" } }));
    await page.goto("/");
    await page.getByLabel("Place or address").fill("Eiffel Tower");
    await page.getByLabel("Country codes (optional, e.g. np,in)").fill("fr, be");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(page.getByRole("listitem")).toHaveCount(2);
    await expect(page.getByRole("listitem").first()).toContainText("Eiffel Tower, Paris");
    await expect(page.getByRole("listitem").first()).toContainText("48.858400, 2.294500");
    await expect(page.locator("#meta")).toHaveText(/HTTP 200 · Request time \d+ ms · Cache miss/);
    expect(seen[0]).toBe("/demo/geocode?q=Eiffel+Tower&limit=5&countrycodes=fr%2Cbe");
  });

  test("upstream text is rendered as text, not HTML", async ({ page }) => {
    const hostile = '<img src=x onerror="window.pwned=1">';
    await mockApi(page, () => ({ body: collection([feature(hostile, 1, 2)]) }));
    await page.goto("/");
    await page.getByLabel("Place or address").fill("anything");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(page.locator(".result-label")).toHaveText(hostile);
    await expect(page.locator(".results img")).toHaveCount(0);
    expect(await page.evaluate(() => (window as unknown as { pwned?: number }).pwned)).toBeUndefined();
  });

  test("reverse lookup sends coordinates and shows the address", async ({ page }) => {
    const seen = await mockApi(page, () => ({ body: collection([feature("Champ de Mars, Paris", 2.2945, 48.8584)]) }));
    await page.goto("/");
    await page.getByRole("tab", { name: "Reverse lookup" }).click();
    await expect(page.getByRole("status")).toHaveText("Enter a latitude and longitude to begin.");
    await page.getByRole("button", { name: "48.8584, 2.2945" }).click();
    await page.getByRole("button", { name: "Look up address" }).click();
    await expect(page.locator(".result-label")).toHaveText("Champ de Mars, Paris");
    expect(seen[0]).toBe("/demo/reverse?lat=48.8584&lon=2.2945");
  });

  test("empty results", async ({ page }) => {
    await mockApi(page, () => ({ body: collection([]) }));
    await page.goto("/");
    await page.getByLabel("Place or address").fill("zzzz");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(page.getByRole("status")).toHaveText("No matching locations found. Try a more specific search.");
  });

  test("rate limit shows the wait time from Retry-After", async ({ page }) => {
    await mockApi(page, () => ({
      status: 429,
      headers: { "Retry-After": "30" },
      body: { error: { code: "rate_limited", message: "x", request_id: "req_1" } },
    }));
    await page.goto("/");
    await page.getByLabel("Place or address").fill("Paris");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(page.getByRole("status")).toHaveText(
      "Too many requests. Please wait before trying again. You can try again in 30 seconds.",
    );
    await expect(page.locator("#meta")).toContainText("HTTP 429");
  });

  test("quota exhaustion shows when it resets", async ({ page }) => {
    await mockApi(page, () => ({
      status: 503,
      headers: { "Retry-After": "7200" },
      body: { error: { code: "quota_exhausted", message: "x", request_id: "req_1" } },
    }));
    await page.goto("/");
    await page.getByLabel("Place or address").fill("Paris");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(page.getByRole("status")).toContainText(
      "The demo has reached its usage limit. Please try again after the quota resets. It resets at",
    );
  });

  test("network failure shows the unavailable message", async ({ page }) => {
    await mockApi(page, () => "abort");
    await page.goto("/");
    await page.getByLabel("Place or address").fill("Paris");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(page.getByRole("status")).toHaveText("The service is temporarily unavailable. Please try again shortly.");
  });

  test("invalid input shows the API's message", async ({ page }) => {
    await mockApi(page, () => ({
      status: 400,
      body: { error: { code: "invalid_request", message: "Parameter 'lat' must be between -90 and 90.", request_id: "r" } },
    }));
    await page.goto("/");
    await page.getByRole("tab", { name: "Reverse lookup" }).click();
    await page.getByLabel("Latitude").fill("123");
    await page.getByLabel("Longitude").fill("0");
    await page.getByRole("button", { name: "Look up address" }).click();
    await expect(page.getByRole("status")).toHaveText("Parameter 'lat' must be between -90 and 90.");
  });

  test("a slow response shows a waiting note, then the results", async ({ page }) => {
    await mockApi(page, () => ({ body: RESULTS, delayMs: 4000 }));
    await page.goto("/");
    await page.getByLabel("Place or address").fill("Paris");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(page.locator(".output")).toHaveAttribute("aria-busy", "true");
    await expect(page.getByRole("status")).toContainText("Waiting for the server", { timeout: 3800 });
    await expect(page.getByRole("listitem")).toHaveCount(2, { timeout: 6000 });
    await expect(page.locator(".output")).toHaveAttribute("aria-busy", "false");
  });

  test("a newer search replaces an older one that is still loading", async ({ page }) => {
    await mockApi(page, (url) =>
      url.searchParams.get("q") === "first"
        ? { body: collection([feature("FIRST RESULT", 1, 1)]), delayMs: 1500 }
        : { body: collection([feature("SECOND RESULT", 2, 2)]) },
    );
    await page.goto("/");
    await page.getByLabel("Place or address").fill("first");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await page.getByLabel("Place or address").fill("second");
    await page.getByLabel("Place or address").press("Enter");
    await expect(page.locator(".result-label")).toHaveText("SECOND RESULT");
    await page.waitForTimeout(2000);
    await expect(page.locator(".result-label")).toHaveText("SECOND RESULT");
  });

  test("copy coordinates and copy JSON", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await mockApi(page, () => ({ body: RESULTS }));
    await page.goto("/");
    await page.getByLabel("Place or address").fill("Eiffel");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await page.getByRole("button", { name: "Copy coordinates" }).first().click();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("48.858400, 2.294500");
    await page.getByText("Response JSON").click();
    await page.getByRole("button", { name: "Copy JSON" }).click();
    const copied = JSON.parse(await page.evaluate(() => navigator.clipboard.readText()));
    expect(copied.features).toHaveLength(2);
  });

  test("tabs work from the keyboard", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("tab", { name: "Address search" }).focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("tab", { name: "Reverse lookup" })).toBeFocused();
    await expect(page.getByRole("tab", { name: "Reverse lookup" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByLabel("Latitude")).toBeVisible();
    await page.keyboard.press("ArrowLeft");
    await expect(page.getByLabel("Place or address")).toBeVisible();
  });

  test("narrow screens have no horizontal scrolling", async ({ page }) => {
    await mockApi(page, () => ({ body: RESULTS }));
    await page.setViewportSize({ width: 360, height: 740 });
    await page.goto("/");
    await page.getByLabel("Place or address").fill("Eiffel");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await page.getByText("Response JSON").click();
    await expect(page.getByRole("listitem")).toHaveCount(2);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test("no automatic location request", async ({ page, context }) => {
    let asked = false;
    await context.exposeBinding("__noteGeo", () => (asked = true));
    await page.addInitScript(() => {
      navigator.geolocation.getCurrentPosition = () => (window as unknown as { __noteGeo: () => void }).__noteGeo();
    });
    await page.goto("/");
    await page.getByRole("tab", { name: "Reverse lookup" }).click();
    await page.waitForTimeout(300);
    expect(asked).toBe(false);
  });

  test("passes automated accessibility checks", async ({ page }) => {
    await mockApi(page, () => ({ body: RESULTS }));
    await page.goto("/");
    const initial = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
    expect(initial.violations).toEqual([]);
    await page.getByLabel("Place or address").fill("Eiffel");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await page.getByText("Response JSON").click();
    const withResults = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
    expect(withResults.violations).toEqual([]);
  });
});

test.describe("against the real API and local fake provider", () => {
  test("search then repeat hits the shared cache; reverse lookup works", async ({ page, request }) => {
    const q = `Playwright ${Date.now()}`;
    await page.goto("/");
    await page.getByLabel("Place or address").fill(q);
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(page.locator(".result-label").first()).toContainText(q);
    await expect(page.locator("#meta")).toContainText("Cache miss");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(page.locator("#meta")).toContainText("Cache hit");

    await page.getByRole("tab", { name: "Reverse lookup" }).click();
    await page.getByLabel("Latitude").fill("27.7044");
    await page.getByLabel("Longitude").fill("85.3073");
    await page.getByRole("button", { name: "Look up address" }).click();
    await expect(page.locator(".result-coords")).toContainText("27.704400, 85.307300");

    const health = await request.get(`${API}/health/ready`);
    expect(health.status()).toBe(200);
  });
});
