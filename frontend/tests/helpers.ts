import type { Page, Route } from "@playwright/test";

export const API = "http://127.0.0.1:8110";

export function feature(label: string, lon: number, lat: number, id = label) {
  return {
    type: "Feature",
    id: `geoapify:${id}`,
    geometry: { type: "Point", coordinates: [lon, lat] },
    properties: { label, kind: "building", address: {}, provider: "geoapify", provider_place_id: id },
  };
}

export function collection(features: unknown[]) {
  return {
    type: "FeatureCollection",
    query: {},
    features,
    attribution: [
      { text: "Powered by Geoapify", url: "https://www.geoapify.com/" },
      { text: "© OpenStreetMap contributors", url: "https://www.openstreetmap.org/copyright" },
    ],
  };
}

type Reply = { status?: number; body?: unknown; headers?: Record<string, string>; delayMs?: number };

/** Answer API calls from `handler` instead of the network; returns the list of requested URLs. */
export async function mockApi(page: Page, handler: (url: URL) => Reply | "abort"): Promise<string[]> {
  const seen: string[] = [];
  await page.route(`${API}/**`, async (route: Route) => {
    const url = new URL(route.request().url());
    seen.push(url.pathname + url.search);
    const reply = handler(url);
    if (reply === "abort") return route.abort("connectionrefused");
    if (reply.delayMs) await new Promise((r) => setTimeout(r, reply.delayMs));
    await route
      .fulfill({
        status: reply.status ?? 200,
        contentType: "application/json",
        headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Expose-Headers": "*", ...reply.headers },
        body: JSON.stringify(reply.body ?? {}),
      })
      .catch(() => undefined); // the page may have cancelled the request already
  });
  return seen;
}
