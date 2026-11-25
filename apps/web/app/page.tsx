import Link from "next/link";
import { ArrowRight, KeyRound, LineChart, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";

const features = [
  {
    icon: ShieldCheck,
    title: "Secure by default",
    body: "Strict CSP, HSTS, and no-referrer defaults ship on every response.",
  },
  {
    icon: KeyRound,
    title: "Key management",
    body: "Issue, inspect, and revoke gateway keys without exposing secrets.",
  },
  {
    icon: LineChart,
    title: "Usage visibility",
    body: "Track request and token spend per key, per day, per model.",
  },
];

export default function LandingPage() {
  return (
    <div className="flex flex-col gap-16">
      <section className="flex max-w-3xl flex-col gap-6">
        <span className="w-fit rounded-full border border-border bg-muted px-3 py-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Phase 1 preview
        </span>
        <h1 className="text-4xl font-semibold tracking-tight sm:text-5xl">
          One gateway for every AI model API
        </h1>
        <p className="text-lg text-muted-foreground">
          Route OpenAI-compatible traffic through a single audited endpoint,
          manage keys, and watch usage in one place.
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <Button asChild size="lg">
            <Link href="/dashboard">
              Open dashboard
              <ArrowRight aria-hidden="true" />
            </Link>
          </Button>
          <Button asChild size="lg" variant="outline">
            <Link href="/docs">Read the docs</Link>
          </Button>
        </div>
      </section>

      <section aria-labelledby="features-heading" className="flex flex-col gap-6">
        <h2
          id="features-heading"
          className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
        >
          What ships in this milestone
        </h2>
        <ul className="grid gap-4 sm:grid-cols-3">
          {features.map(({ icon: Icon, title, body }) => (
            <li
              key={title}
              className="rounded-lg border border-border bg-card p-5 text-card-foreground"
            >
              <Icon className="mb-3 size-5 text-primary" aria-hidden="true" />
              <h3 className="font-medium">{title}</h3>
              <p className="mt-1 text-sm text-muted-foreground">{body}</p>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
