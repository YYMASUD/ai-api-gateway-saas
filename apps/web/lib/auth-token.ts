/**
 * Phase 2 interim token storage helper.
 *
 * SECURITY NOTE: This module uses `sessionStorage` as a temporary measure for
 * Phase 2. Production deployments MUST migrate to an HttpOnly cookie set by a
 * BFF / server action so the JWT is never accessible to client-side scripts.
 *
 * Rules enforced here:
 * - Never use `localStorage`; tokens must not survive tab closure.
 * - Never log or serialize the token value.
 * - Guard every access with `typeof window` so SSR renders stay safe.
 */

const STORAGE_KEY = "ai_gateway_access_token";

/** Return the current bearer token, or null when running on the server / empty. */
export function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(STORAGE_KEY);
  } catch {
    // Storage may be disabled (private mode, iframe sandbox). Treat as empty.
    return null;
  }
}

/** Persist a freshly issued access token. Never call this with a refresh token. */
export function setAccessToken(token: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(STORAGE_KEY, token);
  } catch {
    // Swallow write failures; callers should handle missing tokens gracefully.
  }
}

/** Clear any stored token (sign-out, 401 response, navigation away from auth). */
export function clearAccessToken(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Nothing to do.
  }
}
