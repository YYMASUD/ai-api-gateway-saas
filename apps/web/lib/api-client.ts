/**
 * Typed client for the FastAPI gateway.
 *
 * Security rules for this module:
 * - The base URL comes from `NEXT_PUBLIC_API_BASE_URL` only. It is public by
 *   design (it ends up in the browser bundle), so it must never contain
 *   credentials.
 * - No API key, token, or secret is baked into this file. Callers pass an
 *   optional bearer token per request; it is never persisted here.
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

export interface ApiKey {
  id: string;
  name: string;
  /** Only a masked preview should ever come back from the server. */
  masked_key?: string;
  created_at?: string;
  last_used_at?: string | null;
  revoked_at?: string | null;
}

export interface CreateKeyRequest {
  name: string;
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

interface ApiErrorBody {
  detail?: string;
  error?: string;
  message?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
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

export interface ApiClient {
  readonly baseUrl: string;
  request<T>(path: string, options?: RequestOptions): Promise<T>;
  /** Gateway liveness probe (`GET /healthz`), implemented in Phase 1. */
  health(): Promise<HealthStatus>;
  listModels(): Promise<ModelListResponse>;
  /** Later-phase placeholder: `GET /v1/keys` is not implemented yet. */
  listKeys(): Promise<ApiKey[]>;
  /** Later-phase placeholder: `POST /v1/keys` is not implemented yet. */
  createKey(body: CreateKeyRequest): Promise<ApiKey>;
  /** Later-phase placeholder: `DELETE /v1/keys/{id}` is not implemented yet. */
  revokeKey(id: string): Promise<void>;
  /** Later-phase placeholder: `GET /v1/billing/summary` is not implemented yet. */
  getBillingSummary(): Promise<BillingSummary>;
  /** Later-phase placeholder: `GET /v1/usage` is not implemented yet. */
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
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    if (signal) {
      if (signal.aborted) {
        controller.abort();
      } else {
        signal.addEventListener("abort", () => controller.abort(), {
          once: true,
        });
      }
    }

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
      throw new ApiError(
        cause instanceof Error && cause.name === "AbortError"
          ? `Request to ${url} timed out after ${timeoutMs}ms`
          : `Network request to ${url} failed`,
        0,
        cause,
      );
    } finally {
      clearTimeout(timeout);
    }

    if (response.status === 204) {
      // Void endpoints (e.g. DELETE) resolve to undefined.
      return undefined as unknown as T;
    }

    const contentType = response.headers.get("content-type") ?? "";
    const payload: unknown = contentType.includes("application/json")
      ? await response.json().catch(() => null)
      : await response.text().catch(() => null);

    if (!response.ok) {
      throw new ApiError(
        extractErrorMessage(payload) ?? `Request failed with ${response.status}`,
        response.status,
        payload,
      );
    }

    return payload as T;
  }

  return {
    baseUrl,
    request,
    health: () => request<HealthStatus>("/healthz"),
    listModels: () => request<ModelListResponse>("/v1/models"),
    // Later-phase placeholders below: the gateway does not serve the
    // keys/billing/usage routes yet. Paths are reserved so this typed surface
    // stays stable when those phases land.
    listKeys: () => request<ApiKey[]>("/v1/keys"),
    createKey: (body) => request<ApiKey>("/v1/keys", { method: "POST", body }),
    revokeKey: (id) =>
      request<void>(`/v1/keys/${encodeURIComponent(id)}`, {
        method: "DELETE",
      }),
    getBillingSummary: () => request<BillingSummary>("/v1/billing/summary"),
    getUsage: () => request<UsageSummary>("/v1/usage"),
  };
}

function extractErrorMessage(payload: unknown): string | undefined {
  if (!payload || typeof payload !== "object") return undefined;
  const body = payload as ApiErrorBody;
  return body.detail ?? body.error ?? body.message;
}

/** Shared client for server components / route handlers. */
export const apiClient = createApiClient();
