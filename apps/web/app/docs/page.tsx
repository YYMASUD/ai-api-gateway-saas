import type { Metadata } from "next";

import { PageHeader } from "@/components/page-header";

export const metadata: Metadata = { title: "API documentation" };

const openAiExample = `// Server-side example: keep credentials out of the browser bundle.
const baseUrl =
  process.env.API_INTERNAL_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://localhost:8000";

const response = await fetch(\`\${baseUrl}/v1/chat/completions\`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    Authorization: \`Bearer \${process.env.GATEWAY_API_KEY ?? ""}\`,
  },
  body: JSON.stringify({
    model: "gateway-default",
    messages: [{ role: "user", content: "Hello" }],
  }),
});

const completion = await response.json();`;

const keyQuickstart = `# 1. Health check — no auth required
curl "$NEXT_PUBLIC_API_BASE_URL/healthz"

# 2. Register (or login) to get a dashboard JWT
curl -X POST "$NEXT_PUBLIC_API_BASE_URL/auth/register" \\
  -H "Content-Type: application/json" \\
  -d '{"email":"ops@example.com","password":"<dashboard-password>"}'

# 3. Issue a scoped key (JWT in Authorization header)
curl -X POST "$NEXT_PUBLIC_API_BASE_URL/dashboard/keys" \\
  -H "Authorization: Bearer $ACCESS_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"name":"ci-runner","scopes":["chat:completions","models:read"]}'
# The response includes the full key ONCE. Copy it; it will not be shown again.

# 4. Use the issued key against the OpenAI-compatible surface
curl "$NEXT_PUBLIC_API_BASE_URL/v1/models" \\
  -H "Authorization: Bearer $AI_GATEWAY_API_KEY"`;

export default function DocsPage() {
  return (
    <div className="flex flex-col gap-8">
      <PageHeader
        title="API documentation"
        description="Use the gateway from any OpenAI-compatible client."
      />

      <section aria-labelledby="quickstart-heading" className="flex flex-col gap-4">
        <h2 id="quickstart-heading" className="text-xl font-semibold">
          OpenAI-compatible quickstart
        </h2>
        <p className="max-w-2xl text-sm text-muted-foreground">
          Point your existing SDK at the gateway base URL. The base URL is read
          from <code className="font-mono">NEXT_PUBLIC_API_BASE_URL</code> and
          defaults to <code className="font-mono">http://localhost:8000</code>{" "}
          for local development. Credentials are read from a server-only
          environment variable; never paste a key into source code or client
          components.
        </p>
        <p
          role="note"
          className="max-w-2xl rounded-lg border border-border bg-muted/40 p-4 text-sm text-muted-foreground"
        >
          <code className="font-mono">NEXT_PUBLIC_*</code> values are public and
          are bundled into the browser, so they must never contain credentials.
          Keep <code className="font-mono">GATEWAY_API_KEY</code> server-only.
        </p>
        <pre className="overflow-x-auto rounded-lg border border-border bg-muted/40 p-4 text-sm">
          <code>{openAiExample}</code>
        </pre>
      </section>

      <section aria-labelledby="auth-heading" className="flex flex-col gap-4">
        <h2 id="auth-heading" className="text-xl font-semibold">
          Authentication &amp; key management (Phase 2)
        </h2>
        <p className="max-w-2xl text-sm text-muted-foreground">
          The gateway exposes a self-contained auth surface for dashboard users
          and a separate key-management API scoped to those users. Passwords are
          hashed server-side with a salted KDF, dashboard JWTs are signed with
          HS256 and short-lived, and API keys are stored only as salted hashes.
          Neither plaintext passwords nor raw key material are ever persisted or
          logged.
        </p>
        <ul className="grid gap-3 text-sm text-muted-foreground sm:grid-cols-2">
          <li>
            <code className="font-mono">POST /auth/register</code> — create an
            account and receive a short-lived bearer JWT.
          </li>
          <li>
            <code className="font-mono">POST /auth/login</code> — exchange
            credentials for a JWT. Returns a generic 401 for bad credentials or
            a suspended account (the two are intentionally indistinguishable).
          </li>
          <li>
            <code className="font-mono">GET /auth/me</code> — resolve the
            current user from the bearer token.
          </li>
          <li>
            <code className="font-mono">GET /dashboard/keys</code> — list key
            metadata. Only a masked preview (<code className="font-mono">sk_live_XXXXXXXX</code>)
            is ever returned.
          </li>
          <li>
            <code className="font-mono">POST /dashboard/keys</code> — issue a
            scoped key. The full secret is returned <strong>exactly once</strong>;
            the server stores only the salted hash.
          </li>
          <li>
            <code className="font-mono">DELETE /dashboard/keys/:id</code> —
            revoke a key without downtime.
          </li>
        </ul>
        <p className="max-w-2xl text-sm text-muted-foreground">
          Every error uses the OpenAI-compatible envelope{" "}
          <code className="font-mono">
            {"{ error: { message, type, param, code } }"}</code>
          . Dashboard JWTs are HS256 with mandatory{" "}
          <code className="font-mono">exp</code>,{" "}
          <code className="font-mono">iss</code> and{" "}
          <code className="font-mono">aud</code> claims.
        </p>
        <pre className="overflow-x-auto rounded-lg border border-border bg-muted/40 p-4 text-sm">
          <code>{keyQuickstart}</code>
        </pre>
      </section>

      <section aria-labelledby="endpoints-heading" className="flex flex-col gap-4">
        <h2 id="endpoints-heading" className="text-xl font-semibold">
          Typed client surface
        </h2>
        <p className="max-w-2xl text-sm text-muted-foreground">
          Phase 1 ships gateway liveness at{" "}
          <code className="font-mono">GET /healthz</code>. Phase 2 implements
          auth and key management. Phase 3 adds owner-only credit balance and
          ledger endpoints. Phase 4 adds the OpenAI-compatible surface:{" "}
          <code className="font-mono">GET /v1/models</code> and{" "}
          <code className="font-mono">POST /v1/chat/completions</code>, the
          latter supporting both streaming and non-streaming responses. Billing
          checkout is implemented in Phase 5 through{" "}
          <code className="font-mono">POST /billing/checkout</code> and the
          billing dashboard; usage remains a later-phase placeholder.
        </p>
        <ul className="grid gap-3 text-sm text-muted-foreground sm:grid-cols-2">
          <li>
            <code className="font-mono">health()</code> — gateway liveness via{" "}
            <code className="font-mono">GET /healthz</code>
          </li>
          <li>
            <code className="font-mono">listModels()</code> —{" "}
            <code className="font-mono">GET /v1/models</code> (implemented in
            Phase 4; requires an API key with{" "}
            <code className="font-mono">models:read</code>)
          </li>
          <li>
            <code className="font-mono">register() / login() / me()</code> —
            account lifecycle (implemented in Phase 2)
          </li>
          <li>
            <code className="font-mono">listKeys() / createKey() / revokeKey()</code>{" "}
            — scoped gateway keys (implemented in Phase 2)
          </li>
          <li>
            <code className="font-mono">getCredits() / getCreditLedger()</code>{" "}
            — owner-only balance and ledger (implemented in Phase 3)
          </li>
          <li>
            <code className="font-mono">createCheckout()</code> — Stripe-hosted
            credit-pack checkout (implemented in Phase 5; credits are granted
            only after the verified webhook is processed)
          </li>
          <li>
            <code className="font-mono">getUsage()</code> — usage totals
            (later-phase placeholder)
          </li>
        </ul>
        <p
          role="note"
          className="max-w-2xl rounded-lg border border-border bg-muted/40 p-4 text-sm text-muted-foreground"
        >
          Local development ships a placeholder model{" "}
          <code className="font-mono">gateway-default</code> with zero pricing
          and no configured upstream provider. Real inference calls to{" "}
          <code className="font-mono">POST /v1/chat/completions</code> therefore
          return <code className="font-mono">502</code> until a provider is
          approved.
        </p>
      </section>
    </div>
  );
}
