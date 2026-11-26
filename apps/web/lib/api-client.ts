/**
 * Typed client for the FastAPI gateway.
 *
 * Security rules for this module:
 * - The base URL comes from `NEXT_PUBLIC_API_BASE_URL` only. It is public by
 *   design (it ends up in the browser bundle), so it must never contain
 *   credentials.
 * - No API key, token, or secret is baked into this file. Callers pass an
 *   optional bearer token per client instance; it is never persisted here and
 *   never logged.
 */

const LOCAL_DEFAULT_BASE_URL = "http://localhost:8000";

/** Shape returned by `GET /healthz` on the gateway. */
export interface HealthStatus {
  status: string;
  version?: string;
  uptime_seconds?: number;
}

/** A model exposed through the OpenAI-compatible surface. */
export interface Model {
  id: string;
  object: "model";
  owned_by?: string;
  created?: number;
}

export interface ModelListResponse {
  object: "list";
  data: Model[];
}

/** Account record returned by `/auth/*` and `/auth/me`. */
export interface AuthUser {
  id: string;
  email: string;
  display_name: string | null;
  role: string;
  status: string;
  created_at: string;
}

/** Login/registration envelope. The access token is short-lived. */
export interface TokenResponse {
  access_token: string;
  token_type: "bearer" | string;
  expires_in: number;
  user: AuthUser;
}

export interface RegisterRequest {
  email: string;
  password: string;
  display_name?: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

/** Metadata for an issued gateway key. The secret is only returned on create. */
export interface ApiKey {
  id: string;
  name: string;
  /** Masked preview such as `sk_live_XXXXXXXX`; safe to render. */
  prefix: string;
  scopes: string[];
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

/**
 * Response of `POST /dashboard/keys`; `key` is shown exactly once. The create
 * response also includes `last_used_at` and `revoked_at` (both `null` for a
 * brand-new key). They stay optional here only for tolerance of older
 * deployments; treat their absence as `null`, never as "unknown".
 */
export interface CreatedApiKey {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_at: string;
  /** Full plaintext key. Never persist this value anywhere. */
  key: string;
  last_used_at?: string | null;
  revoked_at?: string | null;
}

export interface CreateKeyRequest {
  name: string;
  scopes?: string[];
}

export interface BillingSummary {
  plan?: string;
  currency?: string;
  balance_cents?: number;
  spend_cents_this_period?: number;
  period_start?: string;
  period_end?: string;
}

export interface UsageRecord {
  date: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
}

export interface UsageSummary {
  total_requests: number;
  total_tokens: number;
  records: UsageRecord[];
}

/**
 * Owner-only wallet state in integer microcredits (`GET /dashboard/credits`).
 * `available_microcredits` equals `balance - reserved`.
 */
export interface CreditBalance {
  balance_microcredits: number;
  reserved_microcredits: number;
  available_microcredits: number;
  lifetime_purchased_microcredits: number;
  lifetime_spent_microcredits: number;
}

/** A single append-only credit ledger entry. */
export interface CreditLedgerEntry {
  id: string;
  type: string;
  amount_microcredits: number;
  reference_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

/** A page of the owner's ledger, newest first. */
export interface CreditLedgerPage {
  items: CreditLedgerEntry[];
  next_cursor: string | null;
}

export interface CreditLedgerQuery {
  /** Page size; the server accepts `1..100` (default 50). */
  limit?: number;
  /** Opaque cursor from a previous `next_cursor`. */
  cursor?: string | null;
}

interface OpenAIErrorEnvelope {
  error?: unknown;
  detail?: unknown;
  message?: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  /** OpenAI-style machine-readable error code, when present. */
  readonly code: string | null;
  /** OpenAI-style error type, when present. */
  readonly type: string | null;

  constructor(
    message: string,
    status: number,
    body: unknown,
    meta: { code?: string | null; type?: string | null } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.code = meta.code ?? null;
    this.type = meta.type ?? null;
  }
}

/**
 * True when a failure means the current session is no longer usable: an
 * expired/invalid token (401) or a suspended account (403 / `user_suspended`).
 * Dashboard surfaces use this to clear the stored token exactly once.
 */
export function isSessionInvalidError(cause: unknown): cause is ApiError {
  if (!(cause instanceof ApiError)) return false;
  if (cause.status === 401) return true;
  return cause.status === 403 || cause.code === "user_suspended";
}

export interface ApiClientOptions {
  /** Overrides `NEXT_PUBLIC_API_BASE_URL`; mainly for tests. */
  baseUrl?: string;
  /** Injectable fetch; defaults to the global fetch. */
  fetchImpl?: typeof fetch;
  /** Bearer token for authenticated routes. Never logged or stored. */
  token?: string;
  /** Per-request timeout in milliseconds. */
  timeoutMs?: number;
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
}

/**
 * Resolve the gateway base URL from an env value, falling back to the local
 * dev default. Trailing slashes are trimmed so path joining stays predictable.
 */
export function resolveApiBaseUrl(rawValue?: string | null): string {
  const candidate = rawValue?.trim();
  if (!candidate) return LOCAL_DEFAULT_BASE_URL;

  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    return LOCAL_DEFAULT_BASE_URL;
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return LOCAL_DEFAULT_BASE_URL;
  }

  return parsed.toString().replace(/\/+$/, "");
}

/** Join a base URL and an API path with exactly one slash. */
export function joinUrl(baseUrl: string, path: string): string {
  const normalizedBase = baseUrl.replace(/\/+$/, "");
  const normalizedPath = `/${path.replace(/^\/+/, "")}`;
  return `${normalizedBase}${normalizedPath}`;
}

/**
 * Abort failures surface as `Error` or `DOMException` depending on the
 * runtime, so detect them by name rather than by prototype chain.
 */
function isAbortError(cause: unknown): boolean {
  return (
    typeof cause === "object" &&
    cause !== null &&
    (cause as { name?: unknown }).name === "AbortError"
  );
}

export interface ApiClient {
  readonly baseUrl: string;
  request<T>(path: string, options?: RequestOptions): Promise<T>;
  /** Gateway liveness probe (`GET /healthz`), implemented in Phase 1. */
  health(): Promise<HealthStatus>;
  listModels(): Promise<ModelListResponse>;
  /** Create an account (`POST /auth/register`). */
  register(body: RegisterRequest): Promise<TokenResponse>;
  /** Exchange credentials for a dashboard JWT (`POST /auth/login`). */
  login(body: LoginRequest): Promise<TokenResponse>;
  /** Resolve the current account from the bearer token (`GET /auth/me`). */
  me(): Promise<AuthUser>;
  /** List key metadata (`GET /dashboard/keys`). Never returns plaintext keys. */
  listKeys(): Promise<ApiKey[]>;
  /** Issue a key (`POST /dashboard/keys`); includes the one-time secret. */
  createKey(body: CreateKeyRequest): Promise<CreatedApiKey>;
  /** Revoke a key (`DELETE /dashboard/keys/{id}`). */
  revokeKey(id: string): Promise<void>;
  /** Owner-only wallet state (`GET /dashboard/credits`), implemented in Phase 3. */
  getCredits(): Promise<CreditBalance>;
  /** Owner-only ledger page (`GET /dashboard/credits/ledger`), newest first. */
  getCreditLedger(query?: CreditLedgerQuery): Promise<CreditLedgerPage>;
  /** Later-phase placeholder: `GET /dashboard/billing` is not implemented yet. */
  getBillingSummary(): Promise<BillingSummary>;
  /** Later-phase placeholder: `GET /dashboard/usage` is not implemented yet. */
  getUsage(): Promise<UsageSummary>;
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  const baseUrl = resolveApiBaseUrl(
    options.baseUrl ?? process.env.NEXT_PUBLIC_API_BASE_URL,
  );
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const timeoutMs = options.timeoutMs ?? 15_000;
  const token = options.token;

  async function request<T>(
    path: string,
    { method = "GET", body, signal }: RequestOptions = {},
  ): Promise<T> {
    if (typeof fetchImpl !== "function") {
      throw new ApiError("Fetch is not available in this runtime", 0, null);
    }

    const url = joinUrl(baseUrl, path);
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (token) headers.Authorization = `Bearer ${token}`;

    const controller = new AbortController();
    // `timedOut` lets us tell our own deadline apart from caller cancellation
    // once both funnel into the same AbortController.
    let timedOut = false;
    const timeout = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);

    const forwardAbort = () => controller.abort();
    if (signal) {
      if (signal.aborted) {
        controller.abort();
      } else {
        signal.addEventListener("abort", forwardAbort, { once: true });
      }
    }

    const toRequestError = (cause: unknown): ApiError => {
      if (isAbortError(cause)) {
        // Our own deadline takes precedence; anything else that aborted the
        // request came from the caller's signal.
        return timedOut
          ? new ApiError(
              `Request to ${url} timed out after ${timeoutMs}ms`,
              0,
              cause,
            )
          : new ApiError(`Request to ${url} was cancelled`, 0, cause);
      }
      return new ApiError(`Network request to ${url} failed`, 0, cause);
    };

    try {
      let response: Response;
      try {
        response = await fetchImpl(url, {
          method,
          headers,
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: controller.signal,
          credentials: "omit",
          cache: "no-store",
        });
      } catch (cause) {
        throw toRequestError(cause);
      }

      if (response.status === 204) {
        // Void endpoints (e.g. DELETE) resolve to undefined.
        return undefined as unknown as T;
      }

      // Keep the timeout armed while the body streams: `fetch` resolves as soon
      // as headers arrive, so a stalled body would otherwise hang forever.
      let payload: unknown;
      try {
        const contentType = response.headers.get("content-type") ?? "";
        payload = contentType.includes("application/json")
          ? await response.json()
          : await response.text();
      } catch (cause) {
        if (isAbortError(cause)) {
          throw toRequestError(cause);
        }
        // Non-JSON or malformed bodies are tolerated as an empty payload.
        payload = null;
      }

      if (!response.ok) {
        const parsed = parseErrorPayload(payload);
        throw new ApiError(
          parsed.message ?? `Request failed with ${response.status}`,
          response.status,
          payload,
          { code: parsed.code, type: parsed.type },
        );
      }

      return payload as T;
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener("abort", forwardAbort);
    }
  }

  return {
    baseUrl,
    request,
    health: () => request<HealthStatus>("/healthz"),
    listModels: () => request<ModelListResponse>("/v1/models"),
    register: (body) =>
      request<TokenResponse>("/auth/register", { method: "POST", body }),
    login: (body) =>
      request<TokenResponse>("/auth/login", { method: "POST", body }),
    me: () => request<AuthUser>("/auth/me"),
    listKeys: () => request<ApiKey[]>("/dashboard/keys"),
    createKey: (body) =>
      request<CreatedApiKey>("/dashboard/keys", { method: "POST", body }),
    revokeKey: (id) =>
      request<void>(`/dashboard/keys/${encodeURIComponent(id)}`, {
        method: "DELETE",
      }),
    getCredits: () => request<CreditBalance>("/dashboard/credits"),
    getCreditLedger: ({ limit, cursor } = {}) => {
      const params = new URLSearchParams();
      if (limit !== undefined) params.set("limit", String(limit));
      if (cursor) params.set("cursor", cursor);
      const query = params.toString();
      return request<CreditLedgerPage>(
        `/dashboard/credits/ledger${query ? `?${query}` : ""}`,
      );
    },
    // Later-phase placeholders below: paths follow the documented future
    // dashboard contract so this typed surface stays stable when they land.
    getBillingSummary: () => request<BillingSummary>("/dashboard/billing"),
    getUsage: () => request<UsageSummary>("/dashboard/usage"),
  };
}

interface ParsedError {
  message?: string;
  code: string | null;
  type: string | null;
}

/**
 * Parse the gateway's OpenAI-compatible error envelope
 * `{ error: { message, type, param, code } }`, while remaining tolerant of
 * plain FastAPI detail strings and legacy shapes.
 */
function parseErrorPayload(payload: unknown): ParsedError {
  if (!payload || typeof payload !== "object") {
    return { code: null, type: null };
  }

  const body = payload as OpenAIErrorEnvelope;
  if (body.error && typeof body.error === "object") {
    const nested = body.error as {
      message?: unknown;
      type?: unknown;
      code?: unknown;
    };
    return {
      message:
        typeof nested.message === "string" ? nested.message : undefined,
      type: typeof nested.type === "string" ? nested.type : null,
      code: typeof nested.code === "string" ? nested.code : null,
    };
  }

  if (typeof body.detail === "string") {
    return { message: body.detail, code: null, type: null };
  }
  if (typeof body.error === "string") {
    return { message: body.error, code: null, type: null };
  }
  if (typeof body.message === "string") {
    return { message: body.message, code: null, type: null };
  }
  return { code: null, type: null };
}

/** Shared client for server components / route handlers. */
export const apiClient = createApiClient();
