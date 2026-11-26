import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import BillingPage from "@/app/dashboard/billing/page";

import { allStorageValues } from "./helpers";

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

const TOKEN = "fake.jwt.token.for.billing.component.tests";
const STORAGE_KEY = "ai_gateway_access_token";
const CHECKOUT_URL = "https://checkout.stripe.test/c/pay/cs_test_phase5";
const UNCONFIGURED_MESSAGE = "Billing is not configured in this environment";

const originalLocationDescriptor = Object.getOwnPropertyDescriptor(
  window,
  "location",
);

let assignMock: ReturnType<typeof vi.fn>;

/** Replace jsdom's real navigator so `assign` can be asserted and restored. */
function installLocation(search = ""): void {
  assignMock = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      search,
      assign: assignMock,
    },
  });
}

const fakeUser = {
  id: "usr_billing",
  email: "qa@example.com",
  display_name: "QA",
  role: "owner",
  status: "active",
  created_at: "2025-01-01T00:00:00Z",
};

const fakeBalance = {
  balance_microcredits: 12_345_678,
  reserved_microcredits: 345_678,
  available_microcredits: 12_000_000,
  lifetime_purchased_microcredits: 20_000_000,
  lifetime_spent_microcredits: 7_654_322,
};

const fakeLedger = {
  items: [
    {
      id: "led_purchase",
      type: "purchase",
      amount_microcredits: 10_000_000,
      reference_id: "pay_123",
      metadata: { checkout_session_id: "cs_test_private_metadata" },
      created_at: "2025-01-05T00:00:00Z",
    },
    {
      id: "led_usage",
      type: "usage",
      amount_microcredits: -123_456,
      reference_id: null,
      metadata: {},
      created_at: "2025-01-04T00:00:00Z",
    },
  ],
  next_cursor: null,
};

const fakeCheckout = {
  checkout_url: CHECKOUT_URL,
  session_id: "cs_test_phase5",
  amount_cents: 1_000,
  credits: 10,
  microcredits: 10_000_000,
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

const unconfigured = {
  error: {
    message: "Stripe is not configured",
    type: "service_unavailable",
    param: null,
    code: "stripe_not_configured",
  },
};

function signIn(token = TOKEN) {
  window.sessionStorage.setItem(STORAGE_KEY, token);
}

type ResponseFactory = () => Response | Promise<Response>;
type CheckoutFactory = (body: unknown) => Response | Promise<Response>;

interface GatewayState {
  me?: ResponseFactory;
  credits?: ResponseFactory;
  ledger?: ResponseFactory;
  checkout?: CheckoutFactory;
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
      if (url.endsWith("/dashboard/credits")) {
        return state.credits ? state.credits() : json(fakeBalance);
      }
      if (url.includes("/dashboard/credits/ledger")) {
        return state.ledger ? state.ledger() : json(fakeLedger);
      }
      if (url.endsWith("/billing/checkout") && method === "POST") {
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        return state.checkout ? state.checkout(body) : json(fakeCheckout);
      }
      return json({ error: { message: `unmocked ${method} ${url}` } }, 500);
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, state };
}

function metricText(label: string): string {
  const term = screen.getByText(label);
  const card = term.closest("div");
  return card?.textContent ?? "";
}

function packCard(price: string): HTMLElement {
  const heading = screen.getByText(price);
  const article = heading.closest("article");
  if (!article) throw new Error(`No pack card found for ${price}`);
  return article;
}

describe("BillingPage", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    installLocation();
  });

  afterEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    if (originalLocationDescriptor) {
      Object.defineProperty(window, "location", originalLocationDescriptor);
    }
  });

  it("renders a sign-in CTA and makes no requests when no token is stored", async () => {
    const { fetchMock } = stubGateway();
    render(<BillingPage />);

    expect(
      await screen.findByRole("heading", {
        name: /sign in to manage billing/i,
      }),
    ).toBeTruthy();
    expect(
      screen.getByRole("link", { name: /go to sign in/i }).getAttribute("href"),
    ).toBe("/login");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("renders integer balances, derived credits, and recent ledger entries", async () => {
    signIn();
    stubGateway();
    render(<BillingPage />);

    expect(await screen.findByText("qa@example.com")).toBeTruthy();

    expect(metricText("Balance")).toContain("12345678 microcredits");
    expect(metricText("Balance")).toContain("12.345678 credits");
    expect(metricText("Reserved")).toContain("345678 microcredits");
    expect(metricText("Reserved")).toContain("0.345678 credits");
    expect(metricText("Available")).toContain("12000000 microcredits");
    expect(metricText("Available")).toContain("12 credits");
    expect(metricText("Lifetime purchased")).toContain(
      "20000000 microcredits",
    );
    expect(metricText("Lifetime purchased")).toContain("20 credits");
    expect(metricText("Lifetime spent")).toContain("7654322 microcredits");
    expect(metricText("Lifetime spent")).toContain("7.654322 credits");

    expect(screen.getByText("purchase")).toBeTruthy();
    expect(screen.getByText("+10000000")).toBeTruthy();
    expect(screen.getByText("pay_123")).toBeTruthy();
    expect(screen.getByText("usage")).toBeTruthy();
    expect(screen.getByText("-123456")).toBeTruthy();
    // Ledger metadata is intentionally not rendered.
    expect(
      screen.queryByText("cs_test_private_metadata"),
    ).toBeNull();
  });

  it("shows the exact price and credits for every pack", async () => {
    signIn();
    stubGateway();
    render(<BillingPage />);
    await screen.findByText("qa@example.com");

    const expected = [
      { price: "$10", credits: 10, microcredits: 10_000_000 },
      { price: "$50", credits: 50, microcredits: 50_000_000 },
      { price: "$100", credits: 100, microcredits: 100_000_000 },
    ];

    for (const pack of expected) {
      const card = packCard(pack.price);
      expect(card.textContent).toContain(`${pack.credits} credits`);
      expect(card.textContent).toContain(`${pack.microcredits} microcredits`);
      expect(
        within(card).getByRole("button", { name: /buy credits/i }),
      ).toBeTruthy();
    }
  });

  it("POSTs pack_10 with the bearer token and navigates to the checkout URL", async () => {
    signIn();
    const { fetchMock } = stubGateway();
    render(<BillingPage />);
    await screen.findByText("qa@example.com");

    fireEvent.click(
      within(packCard("$10")).getByRole("button", { name: /buy credits/i }),
    );

    await waitFor(() => {
      expect(assignMock).toHaveBeenCalledWith(CHECKOUT_URL);
    });

    const checkoutCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith("/billing/checkout"),
    );
    expect(checkoutCall).toBeTruthy();
    const [url, init] = checkoutCall!;
    expect(String(url)).toBe("http://localhost:8000/billing/checkout");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ pack_id: "pack_10" });
    expect(init?.headers).toMatchObject({
      "Content-Type": "application/json",
      Authorization: `Bearer ${TOKEN}`,
    });

    // The Checkout URL is transient and must never be persisted.
    expect(allStorageValues().join("\n")).not.toContain(CHECKOUT_URL);
  });

  it("shows the local unconfigured notice on 503 and does not navigate", async () => {
    signIn();
    stubGateway({ checkout: () => json(unconfigured, 503) });
    render(<BillingPage />);
    await screen.findByText("qa@example.com");

    fireEvent.click(
      within(packCard("$10")).getByRole("button", { name: /buy credits/i }),
    );

    expect((await screen.findByRole("alert")).textContent).toContain(
      UNCONFIGURED_MESSAGE,
    );
    expect(assignMock).not.toHaveBeenCalled();
  });

  it("clears the token and shows the sign-in CTA on a 401", async () => {
    signIn();
    stubGateway({ me: () => json(unauthorized, 401) });
    render(<BillingPage />);

    expect(
      await screen.findByRole("heading", {
        name: /sign in to manage billing/i,
      }),
    ).toBeTruthy();
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("clears the token when checkout returns 401", async () => {
    signIn();
    stubGateway({ checkout: () => json(unauthorized, 401) });
    render(<BillingPage />);
    await screen.findByText("qa@example.com");

    fireEvent.click(
      within(packCard("$10")).getByRole("button", { name: /buy credits/i }),
    );

    expect(
      await screen.findByRole("heading", {
        name: /sign in to manage billing/i,
      }),
    ).toBeTruthy();
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(assignMock).not.toHaveBeenCalled();
  });

  it("shows a neutral success notice for ?checkout=success", async () => {
    installLocation("?checkout=success");
    signIn();
    stubGateway();
    render(<BillingPage />);

    const notice = await screen.findByRole("status");
    expect(notice.textContent).toContain(
      "Payment submitted. Credits appear after the verified webhook is processed.",
    );
    // No optimistic credit grant: the balance still comes from the API.
    expect(await screen.findByText("qa@example.com")).toBeTruthy();
    expect(metricText("Balance")).toContain("12345678 microcredits");
  });

  it("shows a neutral cancelled notice for ?checkout=cancelled", async () => {
    installLocation("?checkout=cancelled");
    signIn();
    stubGateway();
    render(<BillingPage />);

    expect((await screen.findByRole("status")).textContent).toContain(
      "Checkout cancelled.",
    );
  });

  it("does not navigate when the checkout URL is unsafe", async () => {
    signIn();
    stubGateway({
      checkout: () => json({ ...fakeCheckout, checkout_url: "javascript:alert(1)" }),
    });
    render(<BillingPage />);
    await screen.findByText("qa@example.com");

    fireEvent.click(
      within(packCard("$10")).getByRole("button", { name: /buy credits/i }),
    );

    expect((await screen.findByRole("alert")).textContent).toMatch(
      /did not include a valid url/i,
    );
    expect(assignMock).not.toHaveBeenCalled();
  });

  it("does not navigate when the checkout URL is absent", async () => {
    signIn();
    const { checkout_url: _omitted, ...withoutUrl } = fakeCheckout;
    stubGateway({ checkout: () => json(withoutUrl) });
    render(<BillingPage />);
    await screen.findByText("qa@example.com");

    fireEvent.click(
      within(packCard("$10")).getByRole("button", { name: /buy credits/i }),
    );

    expect((await screen.findByRole("alert")).textContent).toMatch(
      /did not include a valid url/i,
    );
    expect(assignMock).not.toHaveBeenCalled();
  });
});
