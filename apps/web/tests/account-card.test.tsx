import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AccountCard } from "@/components/account-card";

import { deferred, flushAsync } from "./helpers";

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

const STORAGE_KEY = "ai_gateway_access_token";
const FAKE_TOKEN = ["fake", "jwt", "token", "for", "component", "tests"].join(
  ".",
);

const fakeUser = {
  id: "usr_1",
  email: "qa@example.com",
  display_name: "QA",
  role: "owner",
  status: "active",
  created_at: "2025-01-01T00:00:00Z",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function signIn(token = FAKE_TOKEN) {
  window.sessionStorage.setItem(STORAGE_KEY, token);
}

describe("AccountCard", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  afterEach(() => {
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  it("shows a sign-in link when there is no token", async () => {
    render(<AccountCard />);

    const link = await screen.findByRole("link", { name: /^sign in$/i });
    expect(link.getAttribute("href")).toBe("/login");
    expect(screen.getByText(/not signed in/i)).toBeTruthy();
  });

  it("shows email and role, then signs out clearing the token", async () => {
    signIn();
    vi.stubGlobal("fetch", vi.fn(async () => json(fakeUser)));

    render(<AccountCard />);

    expect(await screen.findByText("qa@example.com")).toBeTruthy();
    expect(screen.getByText("owner")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /sign out/i }));

    await waitFor(() =>
      expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull(),
    );
    const link = await screen.findByRole("link", { name: /^sign in$/i });
    expect(link.getAttribute("href")).toBe("/login");
  });

  it("clears the token and shows sign-in when /auth/me returns 401", async () => {
    signIn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json(
          {
            error: {
              message: "Not authenticated",
              type: "authentication_error",
              param: null,
              code: "invalid_token",
            },
          },
          401,
        ),
      ),
    );

    render(<AccountCard />);

    const link = await screen.findByRole("link", { name: /^sign in$/i });
    expect(link.getAttribute("href")).toBe("/login");
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("clears the token and shows sign-in on 403 user_suspended", async () => {
    signIn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json(
          {
            error: {
              message: "User suspended",
              type: "authentication_error",
              param: null,
              code: "user_suspended",
            },
          },
          403,
        ),
      ),
    );

    render(<AccountCard />);

    const link = await screen.findByRole("link", { name: /^sign in$/i });
    expect(link.getAttribute("href")).toBe("/login");
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("shows a retryable error for non-auth failures", async () => {
    signIn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json({ error: { message: "Upstream unavailable" } }, 500),
      ),
    );

    render(<AccountCard />);

    expect(await screen.findByText(/upstream unavailable/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /retry/i })).toBeTruthy();
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe(FAKE_TOKEN);
  });

  it("old 401 after new login does not clear new token", async () => {
    signIn("old.token");
    const me = deferred<Response>();
    vi.stubGlobal("fetch", vi.fn(() => me.promise));

    render(<AccountCard />);
    await waitFor(() =>
      expect(vi.mocked(globalThis.fetch)).toHaveBeenCalledTimes(1),
    );

    // A newer session lands before the old request's 401 resolves.
    signIn("newer.token");
    me.resolve(
      json(
        {
          error: {
            message: "Not authenticated",
            type: "authentication_error",
            param: null,
            code: "invalid_token",
          },
        },
        401,
      ),
    );
    await flushAsync();

    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe("newer.token");
    expect(screen.queryByRole("link", { name: /^sign in$/i })).toBeNull();
  });
});
