import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  createApiClient,
  joinUrl,
  resolveApiBaseUrl,
} from "@/lib/api-client";

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
}

describe("resolveApiBaseUrl", () => {
  it("falls back to the local default when unset", () => {
    expect(resolveApiBaseUrl(undefined)).toBe("http://localhost:8000");
    expect(resolveApiBaseUrl(null)).toBe("http://localhost:8000");
    expect(resolveApiBaseUrl("   ")).toBe("http://localhost:8000");
  });

  it("trims whitespace and trailing slashes", () => {
    expect(resolveApiBaseUrl("  https://api.example.com/  ")).toBe(
      "https://api.example.com",
    );
    expect(resolveApiBaseUrl("http://localhost:9000///")).toBe(
      "http://localhost:9000",
    );
  });

  it("rejects malformed and non-http values", () => {
    expect(resolveApiBaseUrl("not a url")).toBe("http://localhost:8000");
    expect(resolveApiBaseUrl("ftp://example.com")).toBe(
      "http://localhost:8000",
    );
    expect(resolveApiBaseUrl("javascript:alert(1)")).toBe(
      "http://localhost:8000",
    );
  });

  it("keeps a base path intact", () => {
    expect(resolveApiBaseUrl("https://example.com/gateway/v1")).toBe(
      "https://example.com/gateway/v1",
    );
  });
});

describe("joinUrl", () => {
  it("inserts exactly one slash", () => {
    expect(joinUrl("http://localhost:8000", "healthz")).toBe(
      "http://localhost:8000/healthz",
    );
    expect(joinUrl("http://localhost:8000/", "/healthz")).toBe(
      "http://localhost:8000/healthz",
    );
    expect(joinUrl("http://localhost:8000///", "///v1/models")).toBe(
      "http://localhost:8000/v1/models",
    );
  });
});

describe("createApiClient", () => {
  it("uses NEXT_PUBLIC_API_BASE_URL when no explicit base is given", () => {
    const previous = process.env.NEXT_PUBLIC_API_BASE_URL;
    process.env.NEXT_PUBLIC_API_BASE_URL = "http://gateway.test:8080/";
    try {
      const client = createApiClient({
        fetchImpl: vi.fn(
          async (_url: string, _init?: RequestInit) =>
            new Response(null, { status: 204 }),
        ) as unknown as typeof fetch,
      });
      expect(client.baseUrl).toBe("http://gateway.test:8080");
    } finally {
      if (previous === undefined) {
        delete process.env.NEXT_PUBLIC_API_BASE_URL;
      } else {
        process.env.NEXT_PUBLIC_API_BASE_URL = previous;
      }
    }
  });

  it("builds the /healthz URL and parses JSON", async () => {
    const fetchImpl = vi.fn(
      async (_url: string, _init?: RequestInit) => jsonResponse({ status: "ok" }),
    );
    const client = createApiClient({
      baseUrl: "https://api.example.com/",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await expect(client.health()).resolves.toEqual({ status: "ok" });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("https://api.example.com/healthz");
    expect(init).toMatchObject({ method: "GET", credentials: "omit" });
  });

  it("targets the documented gateway paths", async () => {
    const fetchImpl = vi.fn(
      async (_url: string, _init?: RequestInit) =>
        jsonResponse({ object: "list", data: [] }),
    );
    const client = createApiClient({
      baseUrl: "http://localhost:8000",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await client.listModels();
    // keys/billing/usage are later-phase placeholder paths, kept stable.
    await client.listKeys();
    await client.getBillingSummary();
    await client.getUsage();

    const urls = fetchImpl.mock.calls.map((call) => call[0]);
    expect(urls).toEqual([
      "http://localhost:8000/v1/models",
      "http://localhost:8000/v1/keys",
      "http://localhost:8000/v1/billing/summary",
      "http://localhost:8000/v1/usage",
    ]);
  });

  it("POSTs JSON bodies and sends a bearer token when provided", async () => {
    const fetchImpl = vi.fn(
      async (_url: string, _init?: RequestInit) =>
        jsonResponse({ id: "key_1", name: "ci" }),
    );
    const client = createApiClient({
      baseUrl: "http://localhost:8000",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      token: "test-token-value",
    });

    await client.createKey({ name: "ci" });

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("http://localhost:8000/v1/keys");
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(JSON.stringify({ name: "ci" }));
    expect(init?.headers).toMatchObject({
      "Content-Type": "application/json",
      Authorization: "Bearer test-token-value",
    });
  });

  it("URL-encodes key ids on revoke", async () => {
    const fetchImpl = vi.fn(
      async (_url: string, _init?: RequestInit) =>
        new Response(null, { status: 204 }),
    );
    const client = createApiClient({
      baseUrl: "http://localhost:8000",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await expect(client.revokeKey("key/with space")).resolves.toBeUndefined();
    expect(fetchImpl.mock.calls[0][0]).toBe(
      "http://localhost:8000/v1/keys/key%2Fwith%20space",
    );
  });

  it("throws ApiError with server detail on non-2xx responses", async () => {
    const fetchImpl = vi.fn(
      async (_url: string, _init?: RequestInit) =>
        jsonResponse({ detail: "invalid key" }, { status: 401 }),
    );
    const client = createApiClient({
      baseUrl: "http://localhost:8000",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await expect(client.listKeys()).rejects.toMatchObject({
      name: "ApiError",
      status: 401,
      message: "invalid key",
    });
    await expect(client.listKeys()).rejects.toBeInstanceOf(ApiError);
  });

  it("wraps network failures in ApiError with status 0", async () => {
    const fetchImpl = vi.fn(async (_url: string, _init?: RequestInit) => {
      throw new TypeError("connect ECONNREFUSED");
    });
    const client = createApiClient({
      baseUrl: "http://localhost:8000",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await expect(client.health()).rejects.toMatchObject({
      status: 0,
      name: "ApiError",
    });
  });

  it("aborts slow requests and surfaces a timeout message", async () => {
    const fetchImpl = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            const error = new Error("aborted");
            error.name = "AbortError";
            reject(error);
          });
        }),
    );
    const client = createApiClient({
      baseUrl: "http://localhost:8000",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      timeoutMs: 5,
    });

    await expect(client.health()).rejects.toThrow(/timed out after 5ms/);
  });
});
