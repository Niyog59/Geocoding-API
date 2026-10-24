import { defineConfig, devices } from "@playwright/test";

// The page is built with VITE_API_BASE_URL pointing at a local API started below.
// Mocked specs intercept that origin; the end-to-end spec lets requests through to
// the real API, which uses Redis and the local fake provider (never a real one).
const API_PORT = 8110;
const FAKE_PORT = 9110;
const REDIS_URL = process.env.E2E_REDIS_URL ?? "redis://127.0.0.1:6379/0";
const python = process.env.PYTHON ?? ".venv/bin/python"; // run from the repository root

export default defineConfig({
  testDir: "tests",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `${python} -m uvicorn tools.fake_upstream:app --port ${FAKE_PORT}`,
      cwd: "..",
      url: `http://127.0.0.1:${FAKE_PORT}/__control/state`,
      reuseExistingServer: false,
    },
    {
      command: `${python} -m uvicorn app.main:create_app --factory --port ${API_PORT} --no-access-log`,
      cwd: "..",
      url: `http://127.0.0.1:${API_PORT}/health/ready`,
      reuseExistingServer: false,
      env: {
        APP_ENV: "test",
        PROVIDER: "geoapify",
        GEOAPIFY_API_KEY: "e2e-fake-provider-key",
        GEOAPIFY_BASE_URL: `http://127.0.0.1:${FAKE_PORT}`,
        PROVIDER_RATE_PER_SECOND: "100",
        REDIS_URL,
        ADMIN_SECRET: "e2e-admin-secret-0123456789abcdef0123",
        CORS_ORIGINS: "http://127.0.0.1:4173",
      },
    },
    {
      command: "npm run build && npm run preview -- --host 127.0.0.1",
      url: "http://127.0.0.1:4173",
      reuseExistingServer: false,
      env: { VITE_API_BASE_URL: `http://127.0.0.1:${API_PORT}` },
    },
  ],
});
