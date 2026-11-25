import { expect, test } from "@playwright/test";

/**
 * Smoke coverage for the Phase 1 shell. These tests only need the Next.js
 * app; no FastAPI service is contacted and no secrets are used.
 */

test("landing page renders and links to docs and dashboard", async ({ page }) => {
  await page.goto("/");

  await expect(
    page.getByRole("heading", { name: /one gateway for every ai model api/i }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: /open dashboard/i })).toHaveAttribute(
    "href",
    "/dashboard",
  );
  await expect(page.getByRole("link", { name: "Read the docs" })).toHaveAttribute(
    "href",
    "/docs",
  );
});

test("login page shows the sign-in form", async ({ page }) => {
  await page.goto("/login");

  await expect(page.getByRole("heading", { name: /welcome back/i })).toBeVisible();
  await expect(page.getByLabel("Email")).toBeVisible();
  await expect(page.getByLabel("Password")).toBeVisible();
  await expect(page.getByRole("button", { name: /^sign in$/i })).toBeVisible();
});

test("dashboard renders overview metrics", async ({ page }) => {
  await page.goto("/dashboard");

  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(page.getByText("Requests (24h)")).toBeVisible();
  await expect(
    page.getByRole("navigation", { name: /dashboard navigation/i }),
  ).toBeVisible();
});

test("docs page documents the OpenAI-compatible example", async ({ page }) => {
  await page.goto("/docs");

  await expect(
    page.getByRole("heading", { name: /api documentation/i }),
  ).toBeVisible();
  await expect(page.getByText("NEXT_PUBLIC_API_BASE_URL").first()).toBeVisible();
  await expect(page.getByText("GET /healthz").first()).toBeVisible();
  await expect(
    page.getByText(/later-phase placeholder/i).first(),
  ).toBeVisible();
});

test("admin placeholder is reachable", async ({ page }) => {
  await page.goto("/admin");

  await expect(
    page.getByRole("heading", { name: "Admin", exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/operator-only controls/i)).toBeVisible();
});

test("responses carry the required security headers", async ({ request }) => {
  const response = await request.get("/");
  const headers = response.headers();

  expect(headers["x-frame-options"]).toBe("DENY");
  expect(headers["referrer-policy"]).toBe("no-referrer");
  expect(headers["x-content-type-options"]).toBe("nosniff");
  expect(headers["content-security-policy"]).toContain("default-src 'self'");
  expect(headers["content-security-policy"]).toContain("frame-ancestors 'none'");
  expect(headers["strict-transport-security"]).toBeTruthy();
});
