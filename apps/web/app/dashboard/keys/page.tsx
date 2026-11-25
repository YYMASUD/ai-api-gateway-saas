import type { Metadata } from "next";

import { PageHeader, PlaceholderPanel } from "@/components/page-header";

export const metadata: Metadata = { title: "API keys" };

export default function KeysPage() {
  return (
    <>
      <PageHeader
        title="API keys"
        description="Issue scoped gateway keys and revoke them without downtime."
      />
      <PlaceholderPanel title="Key list">
        <p>
          Keys fetched via <code className="font-mono">listKeys()</code> will
          appear here. The server only ever returns a masked preview, never the
          full secret.
        </p>
      </PlaceholderPanel>
    </>
  );
}
