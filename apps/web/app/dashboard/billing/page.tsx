import type { Metadata } from "next";

import { PageHeader, PlaceholderPanel } from "@/components/page-header";

export const metadata: Metadata = { title: "Billing" };

export default function BillingPage() {
  return (
    <>
      <PageHeader
        title="Billing"
        description="Plan, balance, and current-period spend."
      />
      <PlaceholderPanel title="Billing summary">
        <p>
          <code className="font-mono">getBillingSummary()</code> is ready in the
          typed client. No payment provider is connected in Phase 1.
        </p>
      </PlaceholderPanel>
    </>
  );
}
