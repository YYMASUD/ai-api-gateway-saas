import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import LoginPage from "@/app/(auth)/login/page";

import { deferred, flushAsync } from "./helpers";

// vi.hoisted so the mock factory can safely reference the spy at import time.
const { push } = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

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

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function tokenResponse(accessToken: string) {
  return {
    access_token: accessToken,
    token_type: "bearer",
    expires_in: 3600,
    user: {
      id: "usr_1",
      email: "qa@example.com",
      display_name: "QA",
      role: "owner",
      status: "active",
      created_at: "2025-01-01T00:00:00Z",
    },
  };
}

const invalidCredentials = {
  error: {
    message: "Invalid credentials",
    type: "authentication_error",
    param: null,
    code: "invalid_credentials",
  },
};

function submitForm() {
  fireEvent.submit(screen.getByRole("form", { name: /sign in form/i }));
}

function fillCredentials(password = "fake-password-for-tests") {
  fireEvent.change(screen.getByLabelText("Email"), {
    target: { value: "qa@example.com" },
  });
  fireEvent.change(screen.getByLabelText("Password"), {
    target: { value: password },
  });
}

describe("LoginPage", () => {
  beforeEach(() => {
    push.mockReset();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  it("shows a generic invalid-credentials error on 401", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(invalidCredentials, 401)));

    render(<LoginPage />);
    fillCredentials("wrong-password");
    submitForm();

    expect(await screen.findByText(/invalid email or password/i)).toBeTruthy();
    expect(push).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("renders 403 as a generic error, with no suspension-specific copy", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(invalidCredentials, 403)),
    );

    render(<LoginPage />);
    fillCredentials();
    submitForm();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent ?? "").not.toMatch(/suspended/i);
    expect(push).not.toHaveBeenCalled();
  });

  it("clears a stale token when a fresh sign-in fails", async () => {
    window.sessionStorage.setItem(STORAGE_KEY, "stale.token");
    vi.stubGlobal("fetch", vi.fn(async () => json(invalidCredentials, 401)));

    render(<LoginPage />);
    fillCredentials();
    submitForm();

    await screen.findByText(/invalid email or password/i);
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("does not store an unvalidated token response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ token_type: "bearer", expires_in: 3600 }, 200)),
    );

    render(<LoginPage />);
    fillCredentials();
    submitForm();

    expect(await screen.findByText(/sign in failed/i)).toBeTruthy();
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(push).not.toHaveBeenCalled();
  });

  it("stale login success does not overwrite a newer token", async () => {
    const first = deferred<Response>();
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => {
        call += 1;
        return call === 1
          ? first.promise
          : Promise.resolve(json(tokenResponse("newer.token")));
      }),
    );

    render(<LoginPage />);
    fillCredentials();
    // Two overlapping submissions: the older one resolves last.
    submitForm();
    submitForm();

    await waitFor(() =>
      expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe("newer.token"),
    );

    first.resolve(json(tokenResponse("older.token")));
    await flushAsync();

    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe("newer.token");
    expect(push).toHaveBeenCalledTimes(1);
  });

  it("ignores a success that lands after unmount", async () => {
    const first = deferred<Response>();
    vi.stubGlobal("fetch", vi.fn(() => first.promise));

    const { unmount } = render(<LoginPage />);
    fillCredentials();
    submitForm();
    unmount();

    first.resolve(json(tokenResponse("late.token")));
    await flushAsync();

    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(push).not.toHaveBeenCalled();
  });
});
