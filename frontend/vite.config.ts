import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig, loadEnv, type Plugin } from "vite";

// Writes Cloudflare Pages response headers. The content security policy only
// allows the page to call the configured API origin.
function securityHeaders(apiBase: string): Plugin {
  return {
    name: "security-headers",
    apply: "build",
    writeBundle(options) {
      const api = new URL(apiBase).origin;
      const csp = [
        "default-src 'self'",
        `connect-src 'self' ${api}`,
        "img-src 'self' data:",
        "style-src 'self'",
        "script-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
      ].join("; ");
      const headers = [
        "/*",
        `  Content-Security-Policy: ${csp}`,
        "  X-Content-Type-Options: nosniff",
        "  Referrer-Policy: strict-origin-when-cross-origin",
        "  Permissions-Policy: geolocation=(self), camera=(), microphone=()",
        "/assets/*",
        "  Cache-Control: public, max-age=31536000, immutable",
        "",
      ].join("\n");
      writeFileSync(resolve(options.dir ?? "dist", "_headers"), headers);
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  const apiBase = env.VITE_API_BASE_URL || process.env.VITE_API_BASE_URL || "http://localhost:8000";
  return {
    build: { target: "es2022", sourcemap: false },
    plugins: [securityHeaders(apiBase)],
  };
});
