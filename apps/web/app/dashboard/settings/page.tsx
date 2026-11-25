import type { Metadata } from "next";

import { AccountCard } from "@/components/account-card";
import { PageHeader, PlaceholderPanel } from "@/components/page-header";

export const metadata: Metadata = { title: "Settings" };

export default function SettingsPage() {
  return (
    <>
      <PageHeader
        title="Settings"
        description="Workspace profile, retention, and notification preferences."
      />
      <AccountCard />
      <PlaceholderPanel title="Workspace settings">
        <p>
          Profile and security preferences land in a later phase. The account
          card above shows your signed-in identity and lets you sign out.
        </p>
      </PlaceholderPanel>
    </>
  );
}
