import type { Metadata } from "next";

import { PageHeader } from "@/components/page-header";

export const metadata: Metadata = { title: "API documentation" };

const openAiExample = `// Server-side example: keep credentials out of the browser bundle.
const baseUrl =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

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
        <pre className="overflow-x-auto rounded-lg border border-border bg-muted/40 p-4 text-sm">
          <code>{openAiExample}</code>
        </pre>
      </section>

      <section aria-labelledby="endpoints-heading" className="flex flex-col gap-4">
        <h2 id="endpoints-heading" className="text-xl font-semibold">
          Typed client surface
        </h2>
        <p className="max-w-2xl text-sm text-muted-foreground">
          Phase 1 implements gateway liveness at{" "}
          <code className="font-mono">GET /healthz</code>. The keys, billing,
          and usage methods are later-phase placeholders: their paths are
          reserved, but the gateway does not serve them yet.
        </p>
        <ul className="grid gap-3 text-sm text-muted-foreground sm:grid-cols-2">
          <li>
            <code className="font-mono">health()</code> — gateway liveness via{" "}
            <code className="font-mono">GET /healthz</code>
          </li>
          <li>
            <code className="font-mono">listModels()</code> — available models
          </li>
          <li>
            <code className="font-mono">listKeys()</code> — masked key metadata
            (later-phase placeholder)
          </li>
          <li>
            <code className="font-mono">getBillingSummary()</code> — spend
            snapshot (later-phase placeholder)
          </li>
          <li>
            <code className="font-mono">getUsage()</code> — usage totals
            (later-phase placeholder)
          </li>
        </ul>
      </section>
    </div>
  );
}
