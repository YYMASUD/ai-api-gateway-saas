import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import SignupPage from "@/app/(auth)/signup/page";

import { deferred, flushAsync } from "./helpers";

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
      id: "usr_new",
      email: "new@example.com",
      display_name: "New",
      role: "owner",
      status: "active",
      created_at: "2025-01-01T00:00:00Z",
    },
  };
}

const duplicateEmail = {
  error: {
    message: "Email already registered",
    type: "invalid_request_error",
    param: "email",
    code: "email_taken",
  },
};

function submitForm() {
  fireEvent.submit(screen.getByRole("form", { name: /sign up form/i }));
}

function fillForm({
  password = "correct-horse-battery-staple",
  confirm = password,
}: { password?: string; confirm?: string } = {}) {
  fireEvent.change(screen.getByLabelText(/display name/i), {
    target: { value: "New User" },
  });
  fireEvent.change(screen.getByLabelText(/work email/i), {
    target: { value: "new@example.com" },
  });
  fireEvent.change(screen.getByLabelText(/^password$/i), {
    target: { value: password },
  });
  fireEvent.change(screen.getByLabelText(/confirm password/i), {
    target: { value: confirm },
  });
}

describe("SignupPage", () => {
  beforeEach(() => {
    push.mockReset();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  it("enforces API-aligned input limits and hints", () => {
    render(<SignupPage />);

    expect(screen.getByLabelText(/^password$/i).getAttribute("maxlength")).toBe(
      "128",
    );
    expect(
      screen.getByLabelText(/confirm password/i).getAttribute("maxlength"),
    ).toBe("128");
    expect(
      screen.getByLabelText(/display name/i).getAttribute("maxlength"),
    ).toBe("255");
    expect(screen.getByText(/12-128 characters/i)).toBeTruthy();
    expect(screen.getByText(/up to 255 characters/i)).toBeTruthy();
  });

  it("blocks submission when passwords do not match", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(<SignupPage />);
    fillForm({ password: "correct-horse-battery", confirm: "mismatched-value" });
    submitForm();

    expect(screen.getByText(/passwords do not match/i)).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the API message on a duplicate-email 409", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(duplicateEmail, 409)));

    render(<SignupPage />);
    fillForm();
    submitForm();

    expect(await screen.findByText(/email already registered/i)).toBeTruthy();
    expect(push).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("does not store an unvalidated token response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ token_type: "bearer" }, 201)),
    );

    render(<SignupPage />);
    fillForm();
    submitForm();

    expect(await screen.findByText(/registration failed/i)).toBeTruthy();
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(push).not.toHaveBeenCalled();
  });

  it("clears a stale token when registration fails", async () => {
    window.sessionStorage.setItem(STORAGE_KEY, "stale.token");
    vi.stubGlobal("fetch", vi.fn(async () => json(duplicateEmail, 409)));

    render(<SignupPage />);
    fillForm();
    submitForm();

    await screen.findByText(/email already registered/i);
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("stale signup success does not overwrite a newer token", async () => {
    const first = deferred<Response>();
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => {
        call += 1;
        return call === 1
          ? first.promise
          : Promise.resolve(json(tokenResponse("newer.token"), 201));
      }),
    );

    render(<SignupPage />);
    fillForm();
    submitForm();
    submitForm();

    await waitFor(() =>
      expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe("newer.token"),
    );

    first.resolve(json(tokenResponse("older.token"), 201));
    await flushAsync();

    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe("newer.token");
    expect(push).toHaveBeenCalledTimes(1);
  });
});
