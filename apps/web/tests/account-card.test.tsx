import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AccountCard } from "@/components/account-card";

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

const FAKE_TOKEN = "fake.jwt.token.for.component.tests";
const fakeUser = {
  id: "usr_1",
  email: "qa@example.com",
  display_name: "QA",
  role: "owner",
  status: "active",
  created_at: "2025-01-01T00:00:00Z",
};

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
    window.sessionStorage.setItem("ai_gateway_access_token", FAKE_TOKEN);
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify(fakeUser), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<AccountCard />);

    expect(await screen.findByText("qa@example.com")).toBeTruthy();
    expect(screen.getByText("owner")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /sign out/i }));

    await waitFor(() =>
      expect(
        window.sessionStorage.getItem("ai_gateway_access_token"),
      ).toBeNull(),
    );
    const link = await screen.findByRole("link", { name: /^sign in$/i });
    expect(link.getAttribute("href")).toBe("/login");
  });

  it("clears the token and shows sign-in when /auth/me returns 401", async () => {
    window.sessionStorage.setItem("ai_gateway_access_token", FAKE_TOKEN);
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify({
          error: {
            message: "Not authenticated",
            type: "authentication_error",
            param: null,
            code: "invalid_token",
          },
        }),
        { status: 401, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<AccountCard />);

    const link = await screen.findByRole("link", { name: /^sign in$/i });
    expect(link.getAttribute("href")).toBe("/login");
    expect(
      window.sessionStorage.getItem("ai_gateway_access_token"),
    ).toBeNull();
  });
});
