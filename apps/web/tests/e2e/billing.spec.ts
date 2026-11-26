import { expect, type Page, test } from "@playwright/test";

/**
 * Phase 5 billing flows.
 *
 * The FastAPI gateway is mocked with `page.route`, so these tests need only the
 * Next.js app. The token is an obviously fake fixture assembled by
 * concatenation; no real credential or Stripe secret is used.
 */

const API_ORIGIN = "http://localhost:8000";
const FAKE_TOKEN = ["fake", "jwt", "token", "for", "billing", "e2e"].join(".");
const WEB_ORIGIN = "http://127.0.0.1:3100";
const CHECKOUT_URL = "https://checkout.stripe.test/c/pay/cs_test_e2e";

const fakeUser = {
  id: "usr_billing",
  email: "qa@example.com",
  display_name: "QA Bot",
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
      metadata: { checkout_session_id: "cs_test_unused" },
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

const CORS_HEADERS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "authorization, content-type",
  "access-control-allow-methods": "GET, POST, OPTIONS",
};

const unauthorizedBody = {
  error: {
    message: "Not authenticated",
    type: "authentication_error",
    param: null,
    code: "invalid_token",
  },
};

const unconfiguredBody = {
  error: {
    message: "Stripe is not configured",
    type: "service_unavailable",
    param: null,
    code: "stripe_not_configured",
  },
};

interface MockOptions {
  checkoutStatus?: number;
  onCheckout?: (requestBody: unknown) => void;
  meStatus?: number;
}

async function mockGateway(page: Page, options: MockOptions = {}) {
  await page.route(`${API_ORIGIN}/**`, async (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    const json = (body: unknown, status = 200) =>
      route.fulfill({
        status,
        headers: { ...CORS_HEADERS, "content-type": "application/json" },
        body: JSON.stringify(body),
      });

    const protectedRoute =
      url.pathname === "/auth/me" ||
      url.pathname === "/dashboard/credits" ||
      url.pathname === "/dashboard/credits/ledger" ||
      url.pathname === "/billing/checkout";

    if (
      protectedRoute &&
      request.headers().authorization !== `Bearer ${FAKE_TOKEN}`
    ) {
      await json(unauthorizedBody, 401);
      return;
    }

    if (url.pathname === "/auth/me" && method === "GET") {
      if (options.meStatus && options.meStatus !== 200) {
        await json(unauthorizedBody, options.meStatus);
        return;
      }
      await json(fakeUser);
      return;
    }

    if (url.pathname === "/dashboard/credits" && method === "GET") {
      await json(fakeBalance);
      return;
    }

    if (url.pathname === "/dashboard/credits/ledger" && method === "GET") {
      await json(fakeLedger);
      return;
    }

    if (url.pathname === "/billing/checkout" && method === "POST") {
      const body = request.postDataJSON();
      options.onCheckout?.(body);
      if (options.checkoutStatus === 503) {
        await json(unconfiguredBody, 503);
        return;
      }
      await json({
        checkout_url: CHECKOUT_URL,
        session_id: "cs_test_e2e",
        amount_cents: 1_000,
        credits: 10,
        microcredits: 10_000_000,
      });
      return;
    }

    await json(
      {
        error: {
          message: `Unmocked route: ${method} ${url.pathname}`,
          type: "invalid_request_error",
          param: null,
          code: "unmocked",
        },
      },
      500,
    );
  });
}

async function seedToken(page: Page) {
  await page.addInitScript((token) => {
    window.sessionStorage.setItem("ai_gateway_access_token", token);
  }, FAKE_TOKEN);
}

test("billing page shows a sign-in CTA when unauthenticated", async ({ page }) => {
  await page.goto("/dashboard/billing");

  await expect(
    page.getByRole("heading", { name: /sign in to manage billing/i }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: /go to sign in/i }),
  ).toHaveAttribute("href", "/login");
});

test("protected billing mocks reject missing or incorrect Bearer headers", async ({
  page,
}) => {
  await mockGateway(page);
  await page.goto("/");

  const statuses = await page.evaluate(
    async ({ origin, token }) => {
      const statusFor = async (path: string, headers: Record<string, string>) =>
        (await fetch(`${origin}${path}`, { headers })).status;
      return {
        meNone: await statusFor("/auth/me", {}),
        meWrong: await statusFor("/auth/me", {
          authorization: "Bearer wrong.token",
        }),
        meValid: await statusFor("/auth/me", {
          authorization: `Bearer ${token}`,
        }),
        creditsNone: await statusFor("/dashboard/credits", {}),
        ledgerNone: await statusFor("/dashboard/credits/ledger", {}),
        checkoutNone: (
          await fetch(`${origin}/billing/checkout`, { method: "POST" })
        ).status,
      };
    },
    { origin: API_ORIGIN, token: FAKE_TOKEN },
  );

  expect(statuses.meNone).toBe(401);
  expect(statuses.meWrong).toBe(401);
  expect(statuses.meValid).toBe(200);
  expect(statuses.creditsNone).toBe(401);
  expect(statuses.ledgerNone).toBe(401);
  expect(statuses.checkoutNone).toBe(401);
});

test("billing page renders balances, packs, and recent ledger", async ({
  page,
}) => {
  await seedToken(page);
  await mockGateway(page);
  await page.goto("/dashboard/billing");

  await expect(page.getByText("qa@example.com")).toBeVisible();
  await expect(
    page.getByText("12345678 microcredits", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("12.345678 credits", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("345678 microcredits", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("20000000 microcredits", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("7654322 microcredits", { exact: true }),
  ).toBeVisible();

  await expect(page.getByText("$10", { exact: true })).toBeVisible();
  await expect(page.getByText("$50", { exact: true })).toBeVisible();
  await expect(page.getByText("$100", { exact: true })).toBeVisible();
  await expect(
    page.getByText("100 credits", { exact: true }),
  ).toBeVisible();

  await expect(
    page.getByRole("cell", { name: "purchase", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("+10000000")).toBeVisible();
  await expect(page.getByText("pay_123")).toBeVisible();
});

test("clicking a pack POSTs the exact pack_id and redirects to Checkout", async ({
  page,
}) => {
  await seedToken(page);
  let sentBody: unknown = null;
  await mockGateway(page, {
    onCheckout: (body) => {
      sentBody = body;
    },
  });
  await page.goto("/dashboard/billing");
  await expect(page.getByText("qa@example.com")).toBeVisible();

  // The API returns a fake Stripe URL; fulfill it so the test never hits DNS.
  await page.route("https://checkout.stripe.test/**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/html",
      body: "<!doctype html><title>Fake Checkout</title><h1>Fake Stripe Checkout</h1>",
    });
  });

  const pack = page.locator('[data-pack-id="pack_10"]');
  await pack.getByRole("button", { name: /buy credits/i }).click();

  // Stripe Checkout is a cross-origin navigation; only the URL is asserted.
  await page.waitForURL(CHECKOUT_URL);
  await expect(
    page.getByRole("heading", { name: /fake stripe checkout/i }),
  ).toBeVisible();
  expect(page.url()).toBe(CHECKOUT_URL);
  expect(sentBody).toEqual({ pack_id: "pack_10" });
});

test("a 503 shows the local unconfigured state and keeps the page", async ({
  page,
}) => {
  await seedToken(page);
  await mockGateway(page, { checkoutStatus: 503 });
  await page.goto("/dashboard/billing");
  await expect(page.getByText("qa@example.com")).toBeVisible();

  await page
    .locator('[data-pack-id="pack_10"]')
    .getByRole("button", { name: /buy credits/i })
    .click();

  await expect(
    page.getByRole("alert").filter({
      hasText: "Billing is not configured in this environment",
    }),
  ).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`${WEB_ORIGIN}/dashboard/billing`));
});

test("?checkout=success shows a neutral webhook notice", async ({ page }) => {
  await seedToken(page);
  await mockGateway(page);
  await page.goto("/dashboard/billing?checkout=success");

  await expect(page.getByRole("status")).toContainText(
    "Payment submitted. Credits appear after the verified webhook is processed.",
  );
});

test("?checkout=cancelled shows a neutral notice", async ({ page }) => {
  await seedToken(page);
  await mockGateway(page);
  await page.goto("/dashboard/billing?checkout=cancelled");

  await expect(page.getByRole("status")).toContainText("Checkout cancelled.");
});

test("a 401 from the gateway clears the token and shows the CTA", async ({
  page,
}) => {
  await seedToken(page);
  await mockGateway(page, { meStatus: 401 });
  await page.goto("/dashboard/billing");

  await expect(
    page.getByRole("heading", { name: /sign in to manage billing/i }),
  ).toBeVisible();
  const stored = await page.evaluate(() =>
    window.sessionStorage.getItem("ai_gateway_access_token"),
  );
  expect(stored).toBeNull();
});
