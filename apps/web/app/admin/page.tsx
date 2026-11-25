import type { Metadata } from "next";

import { PageHeader, PlaceholderPanel } from "@/components/page-header";

export const metadata: Metadata = { title: "Admin" };

export default function AdminPage() {
  return (
    <>
      <PageHeader
        title="Admin"
        description="Operator-only controls for tenants, quotas, and providers."
      />
      <PlaceholderPanel title="Admin console">
        <p>
          This surface is intentionally empty in Phase 1. Access control and
          audit logging must land before any operator action is exposed.
        </p>
      </PlaceholderPanel>
    </>
  );
}
