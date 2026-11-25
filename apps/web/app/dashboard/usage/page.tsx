import type { Metadata } from "next";

import { PageHeader, PlaceholderPanel } from "@/components/page-header";

export const metadata: Metadata = { title: "Usage" };

export default function UsagePage() {
  return (
    <>
      <PageHeader
        title="Usage"
        description="Requests, tokens, and latency broken down by key and model."
      />
      <PlaceholderPanel title="Usage chart">
        <p>
          Wire a GET request to <code className="font-mono">/v1/usage</code>{" "}
          here once the FastAPI usage endpoint is available. The typed client
          already exposes <code className="font-mono">getUsage()</code>.
        </p>
      </PlaceholderPanel>
    </>
  );
}
