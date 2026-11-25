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

const FAKE_TOKEN = "fake.jwt.token-for-tests-only";
const FAKE_API_KEY = "sk_live_" + ["FAKE", "FAKE", "FAKE"].join("");
const TEST_PASSWORD = ["correct", "horse", "battery", "staple", "42"].join("-");

const fakeUser = {
  id: "usr_1",
  email: "ops@example.com",
  display_name: "Ops",
  role: "owner",
  status: "active",
  created_at: "2025-01-01T00:00:00Z",
};

const fakeTokenResponse = {
  access_token: FAKE_TOKEN,
  token_type: "bearer",
  expires_in: 3600,
  user: fakeUser,
};

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
    await client.listKeys();
    await client.getBillingSummary();
    await client.getUsage();

    const urls = fetchImpl.mock.calls.map((call) => call[0]);
    expect(urls).toEqual([
      "http://localhost:8000/v1/models",
      "http://localhost:8000/dashboard/keys",
      "http://localhost:8000/v1/billing/summary",
      "http://localhost:8000/v1/usage",
    ]);
  });

  it("POSTs registration payloads to /auth/register", async () => {
    const fetchImpl = vi.fn(
      async (_url: string, _init?: RequestInit) =>
        jsonResponse(fakeTokenResponse, { status: 201 }),
    );
    const client = createApiClient({
      baseUrl: "http://localhost:8000",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    const result = await client.register({
      email: "ops@example.com",
      password: TEST_PASSWORD,
      display_name: "Ops",
    });

    expect(result.access_token).toBe(FAKE_TOKEN);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("http://localhost:8000/auth/register");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      email: "ops@example.com",
      password: TEST_PASSWORD,
      display_name: "Ops",
    });
  });

  it("POSTs credentials to /auth/login", async () => {
    const fetchImpl = vi.fn(
      async (_url: string, _init?: RequestInit) => jsonResponse(fakeTokenResponse),
    );
    const client = createApiClient({
      baseUrl: "http://localhost:8000",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await expect(
      client.login({
        email: "ops@example.com",
        password: TEST_PASSWORD,
      }),
    ).resolves.toMatchObject({ token_type: "bearer", user: fakeUser });

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("http://localhost:8000/auth/login");
    expect(init?.method).toBe("POST");
  });

  it("sends the bearer token to /auth/me", async () => {
    const fetchImpl = vi.fn(
      async (_url: string, _init?: RequestInit) => jsonResponse(fakeUser),
    );
    const client = createApiClient({
      baseUrl: "http://localhost:8000",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      token: FAKE_TOKEN,
    });

    await expect(client.me()).resolves.toEqual(fakeUser);

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("http://localhost:8000/auth/me");
    expect(init?.method).toBe("GET");
    expect(init?.headers).toMatchObject({
      Authorization: `Bearer ${FAKE_TOKEN}`,
    });
  });

  it("lists keys without leaking secrets", async () => {
    const key = {
      id: "key_1",
      name: "ci",
      prefix: "sk_live_XXXXXXXX",
      scopes: ["chat:completions"],
      last_used_at: null,
      revoked_at: null,
      created_at: "2025-01-01T00:00:00Z",
    };
    const fetchImpl = vi.fn(
      async (_url: string, _init?: RequestInit) => jsonResponse([key]),
    );
    const client = createApiClient({
      baseUrl: "http://localhost:8000",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      token: FAKE_TOKEN,
    });

    await expect(client.listKeys()).resolves.toEqual([key]);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("http://localhost:8000/dashboard/keys");
    expect(init?.headers).toMatchObject({
      Authorization: `Bearer ${FAKE_TOKEN}`,
    });
  });

  it("POSTs JSON bodies and sends a bearer token when provided", async () => {
    const fetchImpl = vi.fn(
      async (_url: string, _init?: RequestInit) =>
        jsonResponse(
          {
            id: "key_1",
            name: "ci",
            prefix: "sk_live_XXXXXXXX",
            key: FAKE_API_KEY,
            scopes: ["chat:completions"],
            created_at: "2025-01-01T00:00:00Z",
          },
          { status: 201 },
        ),
    );
    const client = createApiClient({
      baseUrl: "http://localhost:8000",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      token: FAKE_TOKEN,
    });

    const created = await client.createKey({
      name: "ci",
      scopes: ["chat:completions"],
    });
    expect(created.key).toBe(FAKE_API_KEY);

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("http://localhost:8000/dashboard/keys");
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(
      JSON.stringify({ name: "ci", scopes: ["chat:completions"] }),
    );
    expect(init?.headers).toMatchObject({
      "Content-Type": "application/json",
      Authorization: `Bearer ${FAKE_TOKEN}`,
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
      "http://localhost:8000/dashboard/keys/key%2Fwith%20space",
    );
    expect(fetchImpl.mock.calls[0][1]?.method).toBe("DELETE");
  });

  it("parses OpenAI error envelopes with code and type", async () => {
    const fetchImpl = vi.fn(
      async (_url: string, _init?: RequestInit) =>
        jsonResponse(
          {
            error: {
              message: "Invalid credentials",
              type: "authentication_error",
              param: null,
              code: "invalid_credentials",
            },
          },
          { status: 401 },
        ),
    );
    const client = createApiClient({
      baseUrl: "http://localhost:8000",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await expect(
      client.login({ email: "a@b.co", password: "nope" }),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 401,
      message: "Invalid credentials",
      code: "invalid_credentials",
      type: "authentication_error",
    });
    await expect(
      client.login({ email: "a@b.co", password: "nope" }),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("still understands legacy FastAPI detail errors", async () => {
    const fetchImpl = vi.fn(
      async (_url: string, _init?: RequestInit) =>
        jsonResponse({ detail: "invalid key" }, { status: 401 }),
    );
    const client = createApiClient({
      baseUrl: "http://localhost:8000",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await expect(client.listKeys()).rejects.toMatchObject({
      status: 401,
      message: "invalid key",
    });
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
