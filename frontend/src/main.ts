import "./styles.css";

type Attribution = { text: string; url: string };
type Feature = {
  id: string;
  geometry: { type: "Point"; coordinates: [number, number] };
  properties: { label: string; kind?: string | null };
};
type Collection = { type: "FeatureCollection"; features: Feature[]; attribution?: Attribution[] };
type ApiErrorBody = { error?: { code?: string; message?: string; request_id?: string } };

const API = (import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "");
const PROVIDER = import.meta.env.VITE_PROVIDER_NAME ?? "Geoapify";
const SOURCE = import.meta.env.VITE_SOURCE_URL ?? "https://github.com/Niyog59/Geocoding-API";
const SLOW_NOTICE_MS = 3000;
const CLIENT_TIMEOUT_MS = 70000; // a sleeping free-tier backend can take about a minute to start

const TEXT = {
  initialSearch: "Enter a place or address to begin.",
  initialReverse: "Enter a latitude and longitude to begin.",
  empty: "No matching locations found. Try a more specific search.",
  rateLimited: "Too many requests. Please wait before trying again.",
  unavailable: "The service is temporarily unavailable. Please try again shortly.",
  quota: "The demo has reached its usage limit. Please try again after the quota resets.",
  waking: "Waiting for the server. After a period of inactivity it can take up to a minute to start.",
};

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const statusEl = $<HTMLParagraphElement>("status");
const metaEl = $<HTMLParagraphElement>("meta");
const resultsEl = $<HTMLOListElement>("results");
const jsonDetails = $<HTMLDetailsElement>("json-details");
const jsonEl = $<HTMLPreElement>("json");
const copyStatus = $<HTMLParagraphElement>("copy-status");

$<HTMLAnchorElement>("docs-link").href = `${API}/docs`;
$<HTMLAnchorElement>("source-link").href = SOURCE;
renderAttribution([
  { text: `Powered by ${PROVIDER}`, url: "https://www.geoapify.com/" },
  { text: "© OpenStreetMap contributors", url: "https://www.openstreetmap.org/copyright" },
]);
$<HTMLParagraphElement>("privacy").textContent =
  `Searches are sent to this project's API, which forwards them to ${PROVIDER}. ` +
  "Results are cached on the server for up to a day. The API's own logs do not record search text, " +
  "but the hosting services may keep standard request logs.";

// ---- Tabs ---------------------------------------------------------------------------

const tabs = [$<HTMLButtonElement>("tab-search"), $<HTMLButtonElement>("tab-reverse")];
const panels = [$<HTMLElement>("panel-search"), $<HTMLElement>("panel-reverse")];
let currentTab = 0;

function selectTab(index: number, focus = false): void {
  currentTab = index;
  tabs.forEach((tab, i) => {
    tab.setAttribute("aria-selected", String(i === index));
    tab.tabIndex = i === index ? 0 : -1;
    panels[i].hidden = i !== index;
  });
  if (focus) tabs[index].focus();
  cancelInFlight();
  clearResults();
  setStatus(index === 0 ? TEXT.initialSearch : TEXT.initialReverse);
}

tabs.forEach((tab, i) => {
  tab.addEventListener("click", () => selectTab(i));
  tab.addEventListener("keydown", (event) => {
    const moves: Record<string, number> = { ArrowRight: 1, ArrowLeft: -1, Home: -i, End: tabs.length - 1 - i };
    if (event.key in moves) {
      event.preventDefault();
      selectTab((i + moves[event.key] + tabs.length) % tabs.length, true);
    }
  });
});

// ---- Examples fill the form but do not submit ----------------------------------------

document.querySelectorAll<HTMLButtonElement>("button.example").forEach((button) => {
  button.addEventListener("click", () => {
    if (button.dataset.q !== undefined) {
      $<HTMLInputElement>("q").value = button.dataset.q;
      $<HTMLInputElement>("q").focus();
    } else {
      $<HTMLInputElement>("lat").value = button.dataset.lat ?? "";
      $<HTMLInputElement>("lon").value = button.dataset.lon ?? "";
      $<HTMLInputElement>("lat").focus();
    }
  });
});

// ---- Requests --------------------------------------------------------------------------

let controller: AbortController | null = null;
let sequence = 0;
let slowTimer: number | undefined;

function cancelInFlight(): void {
  controller?.abort();
  controller = null;
  window.clearTimeout(slowTimer);
  setBusy(false);
}

function setBusy(busy: boolean): void {
  document.querySelectorAll<HTMLButtonElement>("form button[type=submit]").forEach((b) => (b.disabled = busy));
  document.querySelector("main")?.setAttribute("aria-busy", String(busy));
}

async function run(path: string, params: URLSearchParams): Promise<void> {
  cancelInFlight();
  const mine = ++sequence;
  controller = new AbortController();
  const signal = controller.signal;
  const timeout = window.setTimeout(() => controller?.abort("timeout"), CLIENT_TIMEOUT_MS);
  clearResults();
  setBusy(true);
  setStatus(currentTab === 0 ? "Searching…" : "Looking up…");
  slowTimer = window.setTimeout(() => mine === sequence && setStatus(TEXT.waking), SLOW_NOTICE_MS);
  const started = performance.now();
  try {
    const response = await fetch(`${API}${path}?${params}`, { signal, headers: { Accept: "application/json" } });
    const body: unknown = await response.json().catch(() => null);
    if (mine !== sequence) return; // a newer request replaced this one
    showMeta(response.status, performance.now() - started, response.headers.get("X-Cache"));
    if (response.ok && isCollection(body)) {
      showResults(body);
    } else {
      showError(response, body as ApiErrorBody | null);
    }
  } catch (error) {
    if (mine !== sequence) return;
    if (signal.aborted && signal.reason !== "timeout") return;
    setStatus(TEXT.unavailable, true);
  } finally {
    window.clearTimeout(timeout);
    if (mine === sequence) {
      window.clearTimeout(slowTimer);
      setBusy(false);
      controller = null;
    }
  }
}

function isCollection(body: unknown): body is Collection {
  return typeof body === "object" && body !== null && Array.isArray((body as Collection).features);
}

$<HTMLFormElement>("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const q = $<HTMLInputElement>("q").value.trim();
  if (!q) {
    setStatus(TEXT.initialSearch, true);
    $<HTMLInputElement>("q").focus();
    return;
  }
  const params = new URLSearchParams({ q, limit: $<HTMLSelectElement>("limit").value });
  const countries = $<HTMLInputElement>("countrycodes").value.replace(/\s+/g, "");
  if (countries) params.set("countrycodes", countries);
  void run("/demo/geocode", params);
});

$<HTMLFormElement>("reverse-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const lat = $<HTMLInputElement>("lat").value.trim();
  const lon = $<HTMLInputElement>("lon").value.trim();
  if (!lat || !lon) {
    setStatus(TEXT.initialReverse, true);
    (lat ? $<HTMLInputElement>("lon") : $<HTMLInputElement>("lat")).focus();
    return;
  }
  void run("/demo/reverse", new URLSearchParams({ lat, lon }));
});

$<HTMLButtonElement>("use-location").addEventListener("click", () => {
  if (!("geolocation" in navigator)) {
    setStatus("Location is not available in this browser.", true);
    return;
  }
  setStatus("Requesting your location…");
  navigator.geolocation.getCurrentPosition(
    (position) => {
      $<HTMLInputElement>("lat").value = position.coords.latitude.toFixed(6);
      $<HTMLInputElement>("lon").value = position.coords.longitude.toFixed(6);
      setStatus("Location filled in. Select “Look up address” to continue.");
    },
    () => setStatus("Your location could not be read.", true),
    { timeout: 10000 },
  );
});

// ---- Rendering (upstream data is always set as text, never as HTML) -------------------

function setStatus(text: string, isError = false): void {
  statusEl.textContent = text;
  statusEl.classList.toggle("error", isError);
}

function clearResults(): void {
  resultsEl.replaceChildren();
  metaEl.hidden = true;
  jsonDetails.hidden = true;
  jsonEl.textContent = "";
}

function showMeta(status: number, ms: number, cache: string | null): void {
  const parts = [`HTTP ${status}`, `Request time ${Math.round(ms)} ms`];
  if (cache) parts.push(`Cache ${cache.toLowerCase()}`);
  metaEl.textContent = parts.join(" · ");
  metaEl.hidden = false;
}

function formatCoords(feature: Feature): string {
  const [lon, lat] = feature.geometry.coordinates;
  return `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
}

function showResults(body: Collection): void {
  if (body.attribution?.length) renderAttribution(body.attribution);
  jsonEl.textContent = JSON.stringify(body, null, 2);
  jsonDetails.hidden = false;
  if (body.features.length === 0) {
    setStatus(TEXT.empty);
    return;
  }
  setStatus(`${body.features.length} ${body.features.length === 1 ? "result" : "results"}`);
  for (const feature of body.features) {
    const item = document.createElement("li");
    const label = document.createElement("div");
    label.className = "result-label";
    label.textContent = feature.properties.label;
    const coords = document.createElement("div");
    coords.className = "result-coords";
    coords.textContent = formatCoords(feature) + (feature.properties.kind ? ` · ${feature.properties.kind}` : "");
    const actions = document.createElement("div");
    actions.className = "result-actions";
    const copy = document.createElement("button");
    copy.type = "button";
    copy.textContent = "Copy coordinates";
    copy.addEventListener("click", () => void copyText(formatCoords(feature), "Coordinates copied."));
    const [lon, lat] = feature.geometry.coordinates;
    const map = document.createElement("a");
    map.href = `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=17/${lat}/${lon}`;
    map.target = "_blank";
    map.rel = "noopener noreferrer";
    map.textContent = "View on map";
    actions.append(copy, map);
    item.append(label, coords, actions);
    resultsEl.append(item);
  }
}

function retryAt(response: Response): string {
  const seconds = Number(response.headers.get("Retry-After"));
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  const when = new Date(Date.now() + seconds * 1000);
  const time = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return seconds < 120 ? ` You can try again in ${Math.ceil(seconds)} seconds.` : ` It resets at ${time}.`;
}

function showError(response: Response, body: ApiErrorBody | null): void {
  const code = body?.error?.code;
  if (response.status === 429) {
    setStatus(TEXT.rateLimited + retryAt(response), true);
  } else if (code === "quota_exhausted") {
    setStatus(TEXT.quota + retryAt(response), true);
  } else if (response.status === 400 && body?.error?.message) {
    setStatus(body.error.message, true);
  } else {
    setStatus(TEXT.unavailable, true);
  }
  if (body) {
    jsonEl.textContent = JSON.stringify(body, null, 2);
    jsonDetails.hidden = false;
  }
}

function renderAttribution(items: Attribution[]): void {
  const container = $<HTMLParagraphElement>("attribution");
  container.replaceChildren(
    ...items.map((item) => {
      const link = document.createElement("a");
      link.href = item.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = item.text;
      return link;
    }),
  );
}

async function copyText(text: string, done: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    copyStatus.textContent = done;
  } catch {
    copyStatus.textContent = "Copying is not available in this browser.";
  }
}

$<HTMLButtonElement>("copy-json").addEventListener("click", () => void copyText(jsonEl.textContent ?? "", "JSON copied."));
