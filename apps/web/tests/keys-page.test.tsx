import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import KeysPage from "@/app/dashboard/keys/page";

import { allStorageValues, deferred, flushAsync } from "./helpers";

// jsdom has no App Router context; render Link as a plain anchor.
vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    className,
  }: {
    href: string;
    children?: ReactNode;
    className?: string;
  }) => (
    <a href={href} className={className}>
      {children}
    </a>
  ),
}));

const TOKEN = "fake.jwt.token.for.component.tests";
const STORAGE_KEY = "ai_gateway_access_token";
const FULL_KEY = "sk_live_" + ["FAKE", "FAKE", "FAKE"].join("");

const fakeUser = {
  id: "usr_1",
  email: "qa@example.com",
  display_name: "QA",
  role: "owner",
  status: "active",
  created_at: "2025-01-01T00:00:00Z",
};

const fakeKey = {
  id: "key_1",
  name: "CI runner",
  prefix: "sk_live_XXXXXXXX",
  scopes: ["chat:completions"],
  last_used_at: null,
  revoked_at: null,
  created_at: "2025-01-02T00:00:00Z",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const unauthorized = {
  error: {
    message: "Not authenticated",
    type: "authentication_error",
    param: null,
    code: "invalid_token",
  },
};

const suspended = {
  error: {
    message: "User suspended",
    type: "authentication_error",
    param: null,
    code: "user_suspended",
  },
};

function signIn(token = TOKEN) {
  window.sessionStorage.setItem(STORAGE_KEY, token);
}

interface GatewayState {
  me?: () => Response | Promise<Response>;
  keys?: () => Response | Promise<Response>;
  create?: (body: unknown) => Response | Promise<Response>;
  revoke?: (id: string) => Response | Promise<Response>;
}

/** Instance-level fetch mock; state is read per call so tests can mutate it. */
function stubGateway(initial: GatewayState = {}) {
  const state: GatewayState = { ...initial };
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/auth/me")) {
        return state.me ? state.me() : json(fakeUser);
      }
      if (url.endsWith("/dashboard/keys") && method === "GET") {
        return state.keys ? state.keys() : json([fakeKey]);
      }
      if (url.endsWith("/dashboard/keys") && method === "POST") {
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        return state.create
          ? state.create(body)
          : json(
              {
                id: "key_new",
                name: "Fresh",
                prefix: "sk_live_XXXXXXXX",
                scopes: ["chat:completions"],
                last_used_at: null,
                revoked_at: null,
                created_at: "2025-01-03T00:00:00Z",
                key: FULL_KEY,
              },
              201,
            );
      }
      if (url.includes("/dashboard/keys/") && method === "DELETE") {
        const id = decodeURIComponent(url.split("/").pop() ?? "");
        return state.revoke
          ? state.revoke(id)
          : new Response(null, { status: 204 });
      }
      return json({ error: { message: `unmocked ${method} ${url}` } }, 500);
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, state };
}

function scopeCheckbox(name: string) {
  return screen.getByLabelText(name, { selector: "input" });
}

describe("KeysPage", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
  });

  afterEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders a sign-in CTA when no token is stored", async () => {
    render(<KeysPage />);

    const heading = await screen.findByRole("heading", {
      name: /sign in to manage keys/i,
    });
    expect(heading).toBeTruthy();
    const link = screen.getByRole("link", { name: /go to sign in/i });
    expect(link.getAttribute("href")).toBe("/login");
  });

  it("lists existing keys for an authenticated user", async () => {
    signIn();
    stubGateway();
    render(<KeysPage />);

    expect(await screen.findByText("CI runner")).toBeTruthy();
    expect(screen.getByText("sk_live_XXXXXXXX")).toBeTruthy();
    expect(screen.getByText("qa@example.com")).toBeTruthy();
    expect(screen.getByRole("button", { name: /^revoke$/i })).toBeTruthy();
  });

  it("shows an empty state when the account has no keys", async () => {
    signIn();
    stubGateway({ keys: () => json([]) });
    render(<KeysPage />);

    expect(await screen.findByText(/no keys yet/i)).toBeTruthy();
  });

  it("creates a key, reveals the one-time secret, and never persists it", async () => {
    signIn();
    let sentBody: unknown = null;
    let created = false;
    const { state } = stubGateway({
      create: (body) => {
        sentBody = body;
        created = true;
        return json(
          {
            id: "key_new",
            name: "Fresh",
            prefix: "sk_live_XXXXXXXX",
            scopes: ["chat:completions"],
            last_used_at: null,
            revoked_at: null,
            created_at: "2025-01-03T00:00:00Z",
            key: FULL_KEY,
          },
          201,
        );
      },
    });
    // Initial list shows the seeded key; after create it shows the new one.
    state.keys = () =>
      json(created ? [{ ...fakeKey, id: "key_new", name: "Fresh" }] : [fakeKey]);

    render(<KeysPage />);
    await screen.findByText("CI runner");

    fireEvent.change(screen.getByLabelText(/key name/i), {
      target: { value: "Fresh" },
    });
    // Deselect models:read, leaving chat:completions.
    fireEvent.click(scopeCheckbox("models:read"));
    fireEvent.click(screen.getByRole("button", { name: /^create key$/i }));

    expect(await screen.findByText(FULL_KEY)).toBeTruthy();
    expect(sentBody).toEqual({
      name: "Fresh",
      scopes: ["chat:completions"],
    });
    expect(await screen.findByText("Fresh")).toBeTruthy();

    // The one-time secret lives only in React state.
    expect(allStorageValues().join("\n")).not.toContain(FULL_KEY);

    fireEvent.click(screen.getByRole("button", { name: /i saved this key/i }));
    expect(screen.queryByText(FULL_KEY)).toBeNull();
    expect(allStorageValues().join("\n")).not.toContain(FULL_KEY);
  });

  it("disables create and shows a hint when no scope is selected", async () => {
    signIn();
    stubGateway();
    render(<KeysPage />);
    await screen.findByText("CI runner");

    // A name alone must not enable the button.
    fireEvent.change(screen.getByLabelText(/key name/i), {
      target: { value: "No scopes" },
    });
    fireEvent.click(scopeCheckbox("chat:completions"));
    fireEvent.click(scopeCheckbox("models:read"));

    expect(screen.getByText(/select at least one scope/i)).toBeTruthy();
    const button = screen.getByRole("button", {
      name: /^create key$/i,
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  it("sends exactly the chosen scopes", async () => {
    signIn();
    let sentBody: unknown = null;
    stubGateway({
      create: (body) => {
        sentBody = body;
        return json(
          {
            id: "key_new",
            name: "Scoped",
            prefix: "sk_live_XXXXXXXX",
            scopes: ["models:read"],
            created_at: "2025-01-03T00:00:00Z",
            key: FULL_KEY,
          },
          201,
        );
      },
    });

    render(<KeysPage />);
    await screen.findByText("CI runner");

    fireEvent.change(screen.getByLabelText(/key name/i), {
      target: { value: "Scoped" },
    });
    fireEvent.click(scopeCheckbox("chat:completions"));
    fireEvent.click(screen.getByRole("button", { name: /^create key$/i }));

    await screen.findByText(FULL_KEY);
    expect(sentBody).toEqual({ name: "Scoped", scopes: ["models:read"] });
  });

  it("surfaces a create failure without revealing a key", async () => {
    signIn();
    stubGateway({
      create: () =>
        json(
          {
            error: {
              message: "Key name already exists",
              type: "invalid_request_error",
              param: "name",
              code: "duplicate_name",
            },
          },
          409,
        ),
    });
    render(<KeysPage />);
    await screen.findByText("CI runner");

    fireEvent.change(screen.getByLabelText(/key name/i), {
      target: { value: "Dup" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^create key$/i }));

    expect(await screen.findByText(/key name already exists/i)).toBeTruthy();
    expect(screen.queryByText(FULL_KEY)).toBeNull();
  });

  it("revokes after confirmation and refreshes the list", async () => {
    signIn();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    let revoked: string | null = null;
    const { state } = stubGateway({
      revoke: (id) => {
        revoked = id;
        return new Response(null, { status: 204 });
      },
    });
    render(<KeysPage />);
    await screen.findByText("CI runner");

    state.keys = () =>
      json([{ ...fakeKey, revoked_at: "2025-01-04T00:00:00Z" }]);

    fireEvent.click(screen.getByRole("button", { name: /^revoke$/i }));

    await waitFor(() => expect(revoked).toBe("key_1"));
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Revoked", { exact: true })).toBeTruthy();
  });

  it("cancel in the confirm dialog skips the request", async () => {
    signIn();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const { fetchMock } = stubGateway();
    render(<KeysPage />);
    await screen.findByText("CI runner");
    fetchMock.mockClear();

    fireEvent.click(screen.getByRole("button", { name: /^revoke$/i }));
    await flushAsync();

    const deleteCalls = fetchMock.mock.calls.filter(
      (call) => (call[1]?.method ?? "GET") === "DELETE",
    );
    expect(deleteCalls).toHaveLength(0);
  });

  it("clears the session and shows the CTA on a 401", async () => {
    signIn();
    stubGateway({ me: () => json(unauthorized, 401) });
    render(<KeysPage />);

    expect(
      await screen.findByRole("heading", { name: /sign in to manage keys/i }),
    ).toBeTruthy();
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("clears the session and shows the CTA on a 403 user_suspended", async () => {
    signIn();
    stubGateway({ me: () => json(suspended, 403) });
    render(<KeysPage />);

    expect(
      await screen.findByRole("heading", { name: /sign in to manage keys/i }),
    ).toBeTruthy();
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("clears the session when create returns 401", async () => {
    signIn();
    stubGateway({ create: () => json(unauthorized, 401) });
    render(<KeysPage />);
    await screen.findByText("CI runner");

    fireEvent.change(screen.getByLabelText(/key name/i), {
      target: { value: "Will fail" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^create key$/i }));

    expect(
      await screen.findByRole("heading", { name: /sign in to manage keys/i }),
    ).toBeTruthy();
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("shows a retryable error state for non-auth failures", async () => {
    signIn();
    stubGateway({ me: () => json({ detail: "Database unavailable" }, 500) });
    render(<KeysPage />);

    expect(
      await screen.findByText(/database unavailable/i),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: /retry/i })).toBeTruthy();
    // A server fault must not sign the user out.
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe(TOKEN);
  });

  it("old 401 after new login does not clear new token", async () => {
    signIn("old.token");
    const me = deferred<Response>();
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      return url.endsWith("/auth/me")
        ? me.promise
        : Promise.resolve(json([fakeKey]));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<KeysPage />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    // A newer session lands while the old `/auth/me` is still pending.
    signIn("newer.token");
    me.resolve(json(unauthorized, 401));
    await flushAsync();

    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe("newer.token");
    expect(
      screen.queryByRole("heading", { name: /sign in to manage keys/i }),
    ).toBeNull();
  });
});
