import { expect, type Page, test } from "@playwright/test";

/**
 * Phase 2 auth + key management flows.
 *
 * The FastAPI gateway is mocked with `page.route`, so these tests need only the
 * Next.js app. All credentials/keys below are obviously fake fixtures; no real
 * secret is ever used, and the mocked token is not a valid JWT.
 */

const API_ORIGIN = "http://localhost:8000";
const FAKE_TOKEN = ["fake", "jwt", "token", "for", "e2e", "tests"].join(".");
const FAKE_FULL_KEY = ["sk", "live", "FAKEFAKEFAKE0000000000"].join("_");
const SIGNUP_PASSWORD = ["correct", "horse", "battery", "staple"].join("-");

const fakeUser = {
  id: "usr_e2e",
  email: "qa@example.com",
  display_name: "QA Bot",
  role: "owner",
  status: "active",
  created_at: "2025-01-01T00:00:00Z",
};

const fakeKey: {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
} = {
  id: "key_1",
  name: "CI runner",
  prefix: "sk_live_XXXXXXXX",
  scopes: ["chat:completions", "models:read"],
  last_used_at: null,
  revoked_at: null,
  created_at: "2025-01-02T00:00:00Z",
};

const CORS_HEADERS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "authorization, content-type",
  "access-control-allow-methods": "GET, POST, DELETE, OPTIONS",
};

const unauthorizedBody = {
  error: {
    message: "Not authenticated",
    type: "authentication_error",
    param: null,
    code: "invalid_token",
  },
};

interface MockOptions {
  keys?: Array<typeof fakeKey>;
  onCreateKey?: (requestBody: unknown) => void;
  onRevoke?: (keyId: string) => void;
  loginStatus?: number;
  meStatus?: number;
  registerStatus?: number;
}

async function mockGateway(page: Page, options: MockOptions = {}) {
  const keys = [...(options.keys ?? [fakeKey])];

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
      url.pathname === "/dashboard/keys" ||
      url.pathname.startsWith("/dashboard/keys/");

    if (
      protectedRoute &&
      request.headers().authorization !== `Bearer ${FAKE_TOKEN}`
    ) {
      await json(unauthorizedBody, 401);
      return;
    }

    if (url.pathname === "/auth/login" && method === "POST") {
      if (options.loginStatus && options.loginStatus !== 200) {
        await json(
          {
            error: {
              message: "Invalid credentials",
              type: "authentication_error",
              param: null,
              code: "invalid_credentials",
            },
          },
          options.loginStatus,
        );
        return;
      }
      await json({
        access_token: FAKE_TOKEN,
        token_type: "bearer",
        expires_in: 3600,
        user: fakeUser,
      });
      return;
    }

    if (url.pathname === "/auth/register" && method === "POST") {
      if (options.registerStatus && options.registerStatus !== 201) {
        await json(
          {
            error: {
              message: "Email already registered",
              type: "invalid_request_error",
              param: "email",
              code: "email_taken",
            },
          },
          options.registerStatus,
        );
        return;
      }
      await json(
        {
          access_token: FAKE_TOKEN,
          token_type: "bearer",
          expires_in: 3600,
          user: {
            ...fakeUser,
            email: "new@example.com",
            display_name: "New User",
          },
        },
        201,
      );
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

    if (url.pathname === "/dashboard/keys" && method === "GET") {
      await json(keys);
      return;
    }

    if (url.pathname === "/dashboard/keys" && method === "POST") {
      options.onCreateKey?.(request.postDataJSON());
      const created = {
        id: "key_new",
        name: "Created in test",
        prefix: "sk_live_XXXXXXXX",
        scopes: ["chat:completions", "models:read"],
        created_at: "2025-01-03T00:00:00Z",
        last_used_at: null,
        revoked_at: null,
        key: FAKE_FULL_KEY,
      };
      // List responses must never contain the full key, only metadata.
      keys.unshift({
        id: created.id,
        name: created.name,
        prefix: created.prefix,
        scopes: created.scopes,
        last_used_at: null,
        revoked_at: null,
        created_at: created.created_at,
      });
      await json(created, 201);
      return;
    }

    if (url.pathname.startsWith("/dashboard/keys/") && method === "DELETE") {
      const id = decodeURIComponent(url.pathname.split("/").pop() ?? "");
      options.onRevoke?.(id);
      const entry = keys.find((key) => key.id === id);
      if (entry) entry.revoked_at = "2025-01-04T00:00:00Z";
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
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

async function expectFullKeyAbsentFromStorage(page: Page) {
  const found = await page.evaluate((key) => {
    const values: string[] = [];
    for (const storage of [window.sessionStorage, window.localStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const storageKey = storage.key(index);
        if (storageKey !== null) values.push(storage.getItem(storageKey) ?? "");
      }
    }
    return values.some((value) => value.includes(key));
  }, FAKE_FULL_KEY);
  expect(found).toBe(false);
}

test("keys page shows a sign-in CTA when unauthenticated", async ({ page }) => {
  await page.goto("/dashboard/keys");

  await expect(
    page.getByRole("heading", { name: /sign in to manage keys/i }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: /go to sign in/i }),
  ).toHaveAttribute("href", "/login");
});

test("login stores the token and redirects to keys", async ({ page }) => {
  await mockGateway(page);
  await page.goto("/login");

  await page.getByLabel("Email").fill("qa@example.com");
  await page.getByLabel("Password").fill("fake-password-for-tests");
  await page.getByRole("button", { name: /^sign in$/i }).click();

  await expect(page).toHaveURL(/\/dashboard\/keys$/);
  const stored = await page.evaluate(() =>
    window.sessionStorage.getItem("ai_gateway_access_token"),
  );
  expect(stored).toBe(FAKE_TOKEN);
  await expect(page.getByText("qa@example.com")).toBeVisible();
});

test("signup stores the token and redirects with a contract-shaped response", async ({
  page,
}) => {
  await mockGateway(page);
  await page.goto("/signup");

  await page.getByLabel("Display name").fill("New User");
  await page.getByLabel("Work email").fill("new@example.com");
  await page.getByLabel("Password", { exact: true }).fill(SIGNUP_PASSWORD);
  await page
    .getByLabel("Confirm password", { exact: true })
    .fill(SIGNUP_PASSWORD);
  await page.getByRole("button", { name: /^create account$/i }).click();

  await expect(page).toHaveURL(/\/dashboard\/keys$/);
  const stored = await page.evaluate(() =>
    window.sessionStorage.getItem("ai_gateway_access_token"),
  );
  expect(stored).toBe(FAKE_TOKEN);
  // Protected routes only succeed because the exact Bearer header was sent.
  await expect(page.getByText("qa@example.com")).toBeVisible();
});

test("signup surfaces a duplicate-email 409", async ({ page }) => {
  await mockGateway(page, { registerStatus: 409 });
  await page.goto("/signup");

  await page.getByLabel("Display name").fill("New User");
  await page.getByLabel("Work email").fill("new@example.com");
  await page.getByLabel("Password", { exact: true }).fill(SIGNUP_PASSWORD);
  await page
    .getByLabel("Confirm password", { exact: true })
    .fill(SIGNUP_PASSWORD);
  await page.getByRole("button", { name: /^create account$/i }).click();

  await expect(
    page.getByRole("alert").filter({ hasText: /email already registered/i }),
  ).toBeVisible();
  await expect(page).toHaveURL(/\/signup$/);
});

test("login surfaces an invalid-credentials error", async ({ page }) => {
  await mockGateway(page, { loginStatus: 401 });
  await page.goto("/login");

  await page.getByLabel("Email").fill("qa@example.com");
  await page.getByLabel("Password").fill("wrong-password");
  await page.getByRole("button", { name: /^sign in$/i }).click();

  await expect(
    page.getByRole("alert").filter({ hasText: /invalid email or password/i }),
  ).toBeVisible();
  await expect(page).toHaveURL(/\/login$/);
});

test("protected mock routes reject missing or incorrect Bearer headers", async ({
  page,
}) => {
  await mockGateway(page);
  await page.goto("/");

  const statuses = await page.evaluate(
    async ({ origin, token }) => {
      const statusFor = async (headers: Record<string, string>) =>
        (await fetch(`${origin}/auth/me`, { headers })).status;
      return {
        none: await statusFor({}),
        wrong: await statusFor({ authorization: "Bearer wrong.token" }),
        valid: await statusFor({ authorization: `Bearer ${token}` }),
        keysNone: (
          await fetch(`${origin}/dashboard/keys`)
        ).status,
      };
    },
    { origin: API_ORIGIN, token: FAKE_TOKEN },
  );

  expect(statuses.none).toBe(401);
  expect(statuses.wrong).toBe(401);
  expect(statuses.keysNone).toBe(401);
  expect(statuses.valid).toBe(200);
});

test("keys page lists existing keys for a signed-in user", async ({ page }) => {
  await seedToken(page);
  await mockGateway(page);
  await page.goto("/dashboard/keys");

  await expect(page.getByText("CI runner")).toBeVisible();
  await expect(page.getByText("sk_live_XXXXXXXX")).toBeVisible();
  await expect(page.getByText("chat:completions").first()).toBeVisible();
});

test("creating a key reveals the full key exactly once", async ({ page }) => {
  await seedToken(page);
  let createdBody: unknown = null;
  await mockGateway(page, {
    onCreateKey: (body) => {
      createdBody = body;
    },
  });
  await page.goto("/dashboard/keys");

  await page.getByLabel("Key name").fill("Created in test");
  await page.getByRole("button", { name: /^create key$/i }).click();

  await expect(page.getByRole("status")).toContainText(FAKE_FULL_KEY);
  await expect(page.getByRole("status")).toContainText(/will not be shown again/i);
  expect(createdBody).toMatchObject({
    name: "Created in test",
    scopes: ["chat:completions", "models:read"],
  });
  await expectFullKeyAbsentFromStorage(page);

  // Dismissing the callout removes the secret from the DOM and both stores.
  await page.getByRole("button", { name: /i saved this key/i }).click();
  await expect(page.getByText(FAKE_FULL_KEY)).toHaveCount(0);
  await expectFullKeyAbsentFromStorage(page);

  // Reloading never restores the one-time secret.
  await page.reload();
  await expect(page.getByText(FAKE_FULL_KEY)).toHaveCount(0);
  await expectFullKeyAbsentFromStorage(page);
});

test("revoking a key sends DELETE and refreshes the list", async ({ page }) => {
  await seedToken(page);
  let revokedId: string | null = null;
  await mockGateway(page, {
    onRevoke: (id) => {
      revokedId = id;
    },
  });
  page.on("dialog", (dialog) => void dialog.accept());
  await page.goto("/dashboard/keys");

  await page.getByRole("button", { name: /^revoke$/i }).click();

  await expect.poll(() => revokedId).toBe("key_1");
  await expect(page.getByText("Revoked", { exact: true })).toBeVisible();
});

test("a 401 from the API clears the token and shows the CTA", async ({
  page,
}) => {
  await seedToken(page);
  await mockGateway(page, { meStatus: 401 });
  await page.goto("/dashboard/keys");

  await expect(
    page.getByRole("heading", { name: /sign in to manage keys/i }),
  ).toBeVisible();
  const stored = await page.evaluate(() =>
    window.sessionStorage.getItem("ai_gateway_access_token"),
  );
  expect(stored).toBeNull();
});
