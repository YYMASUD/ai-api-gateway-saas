import type { Metadata } from "next";

import { PageHeader, PlaceholderPanel } from "@/components/page-header";

export const metadata: Metadata = { title: "Settings" };

export default function SettingsPage() {
  return (
    <>
      <PageHeader
        title="Settings"
        description="Workspace profile, retention, and notification preferences."
      />
      <PlaceholderPanel title="Workspace settings">
        <p>
          Profile and security settings land in Phase 2. Nothing sensitive is
          rendered client-side today.
        </p>
      </PlaceholderPanel>
    </>
  );
}
