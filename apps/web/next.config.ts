import type { NextConfig } from "next";

/**
 * Security headers are applied to every route from a single place so that
 * they cannot drift between pages. No secrets are read or embedded here.
 *
 * CSP notes for Phase 1:
 * - Next.js injects inline bootstrap scripts unless a nonce/hash strategy is
 *   wired through middleware, so `script-src` keeps `'unsafe-inline'`.
 * - `'unsafe-eval'` is only added outside production because the dev server
 *   relies on `eval` for HMR/source maps.
 * - `connect-src` is widened to the configured gateway origin so the browser
 *   can call FastAPI; the origin is parsed from NEXT_PUBLIC_API_BASE_URL.
 *   Tighten script-src with nonces before enabling real end-user traffic.
 */
const isProduction = process.env.NODE_ENV === "production";

const FALLBACK_API_ORIGIN = "http://localhost:8000";

function resolveApiOrigin(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  if (!configured) return FALLBACK_API_ORIGIN;
  try {
    const url = new URL(configured);
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      return FALLBACK_API_ORIGIN;
    }
    return url.origin;
  } catch {
    return FALLBACK_API_ORIGIN;
  }
}

function contentSecurityPolicy(): string {
  const scriptSrc = ["'self'", "'unsafe-inline'"];
  if (!isProduction) scriptSrc.push("'unsafe-eval'");

  return [
    "default-src 'self'",
    `script-src ${scriptSrc.join(" ")}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    `connect-src 'self' ${resolveApiOrigin()}`,
    "manifest-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    // Only advertise HTTPS upgrades in production so local http dev keeps working.
    ...(isProduction ? ["upgrade-insecure-requests"] : []),
  ].join("; ");
}

const securityHeaders = [
  {
    key: "Content-Security-Policy",
    value: contentSecurityPolicy(),
  },
  {
    // `max-age=0` in non-production so localhost is never pinned to HTTPS;
    // full HSTS is only advertised by production builds.
    key: "Strict-Transport-Security",
    value: isProduction
      ? "max-age=63072000; includeSubDomains"
      : "max-age=0",
  },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-DNS-Prefetch-Control", value: "off" },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), payment=()",
  },
];

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  output: "standalone",
  async headers() {
    return [
      {
        source: "/:path*",
        headers: securityHeaders,
      },
    ];
  },
};

export default nextConfig;
