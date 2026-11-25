import type { Metadata } from "next";
import Link from "next/link";

import { AccountCard } from "@/components/account-card";
import { PageHeader, PlaceholderPanel } from "@/components/page-header";
import { Button } from "@/components/ui/button";

export const metadata: Metadata = { title: "Dashboard" };

const metrics = [
  { label: "Requests (24h)", value: "—" },
  { label: "Tokens (24h)", value: "—" },
  { label: "Active keys", value: "—" },
  { label: "Error rate", value: "—" },
];

export default function DashboardPage() {
  return (
    <>
      <PageHeader
        title="Overview"
        description="A live snapshot of gateway traffic. Metrics connect in Phase 2."
        action={
          <Button asChild variant="outline" size="sm">
            <Link href="/docs">API quickstart</Link>
          </Button>
        }
      />
      <AccountCard />
      <dl className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {metrics.map((metric) => (
          <div
            key={metric.label}
            className="rounded-lg border border-border bg-card p-5 text-card-foreground"
          >
            <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {metric.label}
            </dt>
            <dd className="mt-2 text-3xl font-semibold tabular-nums">
              {metric.value}
            </dd>
          </div>
        ))}
      </dl>
      <PlaceholderPanel title="Recent activity">
        <p>
          No events yet. The FastAPI gateway will stream request logs here once
          the metrics pipeline is connected.
        </p>
      </PlaceholderPanel>
    </>
  );
}
