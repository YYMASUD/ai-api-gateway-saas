import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import KeysPage from "@/app/dashboard/keys/page";

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

describe("KeysPage", () => {
  afterEach(() => {
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  it("renders a sign-in CTA when no token is stored", async () => {
    window.sessionStorage.clear();
    render(<KeysPage />);

    const heading = await screen.findByRole("heading", {
      name: /sign in to manage keys/i,
    });
    expect(heading).toBeTruthy();
    const link = screen.getByRole("link", { name: /go to sign in/i });
    expect(link.getAttribute("href")).toBe("/login");
  });

  it("lists existing keys for an authenticated user", async () => {
    window.sessionStorage.setItem(
      "ai_gateway_access_token",
      "fake.jwt.token.for.component.tests",
    );
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) {
        return new Response(
          JSON.stringify({
            id: "usr_1",
            email: "qa@example.com",
            display_name: "QA",
            role: "owner",
            status: "active",
            created_at: "2025-01-01T00:00:00Z",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify([
          {
            id: "key_1",
            name: "CI runner",
            prefix: "sk_live_XXXXXXXX",
            scopes: ["chat:completions"],
            last_used_at: null,
            revoked_at: null,
            created_at: "2025-01-02T00:00:00Z",
          },
        ]),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<KeysPage />);

    expect(await screen.findByText("CI runner")).toBeTruthy();
    expect(screen.getByText("sk_live_XXXXXXXX")).toBeTruthy();
    expect(screen.getByText("qa@example.com")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /^revoke$/i }),
    ).toBeTruthy();
  });
});
