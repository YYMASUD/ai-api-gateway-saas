"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import {
  ApiError,
  type AuthUser,
  type CreditBalance,
  type CreditLedgerEntry,
  type CreditPackId,
  createApiClient,
  isSessionInvalidError,
} from "@/lib/api-client";
import { clearAccessToken, getAccessToken } from "@/lib/auth-token";

const MICROCREDITS_PER_CREDIT = 1_000_000n;
const LEDGER_PAGE_SIZE = 5;
const SUCCESS_NOTICE =
  "Payment submitted. Credits appear after the verified webhook is processed.";
const CANCEL_NOTICE = "Checkout cancelled.";
const UNCONFIGURED_MESSAGE = "Billing is not configured in this environment";

interface CreditPack {
  id: CreditPackId;
  price: string;
  amount_cents: number;
  credits: number;
  microcredits: number;
}

/** Exact catalog mirrored from the API's `credit_packs` service. */
const CREDIT_PACKS: readonly CreditPack[] = [
  {
    id: "pack_10",
    price: "$10",
    amount_cents: 1_000,
    credits: 10,
    microcredits: 10_000_000,
  },
  {
    id: "pack_50",
    price: "$50",
    amount_cents: 5_000,
    credits: 50,
    microcredits: 50_000_000,
  },
  {
    id: "pack_100",
    price: "$100",
    amount_cents: 10_000,
    credits: 100,
    microcredits: 100_000_000,
  },
];

type ViewState =
  | { status: "loading" }
  | { status: "unauthenticated" }
  | {
      status: "ready";
      user: AuthUser;
      balance: CreditBalance;
      ledger: CreditLedgerEntry[];
    }
  | { status: "error"; message: string };

interface CheckoutNotice {
  packId: CreditPackId;
  message: string;
}

export default function BillingPage() {
  const [view, setView] = useState<ViewState>({ status: "loading" });
  const [returnNotice, setReturnNotice] = useState<string | null>(null);
  const [selectedPackId, setSelectedPackId] = useState<CreditPackId | null>(
    null,
  );
  const [pendingPackId, setPendingPackId] = useState<CreditPackId | null>(null);
  const [checkoutNotice, setCheckoutNotice] = useState<CheckoutNotice | null>(
    null,
  );

  // Stale-response guards: ignore async results after unmount, and never let a
  // slower load overwrite a newer one.
  const mountedRef = useRef(true);
  const loadIdRef = useRef(0);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    const flag = new URLSearchParams(window.location.search).get("checkout");
    if (flag === "success") setReturnNotice(SUCCESS_NOTICE);
    if (flag === "cancelled") setReturnNotice(CANCEL_NOTICE);
  }, []);

  const signedOut = useCallback(() => {
    setView({ status: "unauthenticated" });
    setSelectedPackId(null);
    setCheckoutNotice(null);
  }, []);

  /**
   * Only a failure from the currently stored token may clear it, so a stale
   * 401/403 cannot sign out a newer session.
   */
  const handleSessionFailure = useCallback(
    (capturedToken: string) => {
      if (getAccessToken() !== capturedToken) return;
      clearAccessToken();
      if (mountedRef.current) signedOut();
    },
    [signedOut],
  );

  const load = useCallback(async () => {
    const token = getAccessToken();
    const loadId = ++loadIdRef.current;
    if (!token) {
      if (mountedRef.current) signedOut();
      return;
    }

    setView({ status: "loading" });
    try {
      const client = createApiClient({ token });
      const [user, balance, ledgerPage] = await Promise.all([
        client.me(),
        client.getCredits(),
        client.getCreditLedger({ limit: LEDGER_PAGE_SIZE }),
      ]);
      if (!mountedRef.current || loadIdRef.current !== loadId) return;
      setView({
        status: "ready",
        user,
        balance,
        ledger: ledgerPage.items,
      });
    } catch (cause) {
      if (!mountedRef.current || loadIdRef.current !== loadId) return;
      if (isSessionInvalidError(cause)) {
        handleSessionFailure(token);
        return;
      }
      setView({
        status: "error",
        message:
          cause instanceof Error && cause.message
            ? cause.message
            : "Unable to load billing details right now.",
      });
    }
  }, [handleSessionFailure, signedOut]);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleBuy(pack: CreditPack) {
    const token = getAccessToken();
    if (!token) {
      signedOut();
      return;
    }

    setSelectedPackId(pack.id);
    setCheckoutNotice(null);
    setPendingPackId(pack.id);
    try {
      const response = await createApiClient({ token }).createCheckout({
        pack_id: pack.id,
      });
      if (!mountedRef.current || getAccessToken() !== token) return;
      if (!isSafeRedirect(response.checkout_url)) {
        setCheckoutNotice({
          packId: pack.id,
          message: "The checkout response did not include a valid URL.",
        });
        return;
      }
      // Stripe hosts Checkout, so a full document navigation is intentional.
      window.location.assign(response.checkout_url);
    } catch (cause) {
      if (!mountedRef.current || getAccessToken() !== token) return;
      if (isSessionInvalidError(cause)) {
        handleSessionFailure(token);
        return;
      }
      const apiMessage =
        cause instanceof ApiError && cause.message
          ? cause.message
          : cause instanceof Error && cause.message
            ? cause.message
            : "Could not start checkout. Please try again.";
      setCheckoutNotice({
        packId: pack.id,
        message:
          cause instanceof ApiError && cause.status === 503
            ? UNCONFIGURED_MESSAGE
            : apiMessage,
      });
    } finally {
      if (mountedRef.current) setPendingPackId(null);
    }
  }

  const returnPanel = returnNotice ? (
    <p
      role="status"
      className="rounded-lg border border-border bg-muted/40 p-4 text-sm text-muted-foreground"
    >
      {returnNotice}
    </p>
  ) : null;

  if (view.status === "unauthenticated") {
    return (
      <>
        <PageHeader
          title="Billing"
          description="Credit balance, recent activity, and credit-pack checkout."
        />
        {returnPanel}
        <section className="rounded-lg border border-border bg-card p-8 text-center text-card-foreground">
          <h2 className="text-lg font-semibold">Sign in to manage billing</h2>
          <p className="mt-2 text-sm text-muted-foreground">
            Authentication is required to view credits or buy a credit pack.
          </p>
          <div className="mt-6">
            <Button asChild>
              <Link href="/login">Go to sign in</Link>
            </Button>
          </div>
        </section>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Billing"
        description="Credit balance, recent activity, and credit-pack checkout."
      />
      {returnPanel}

      {view.status === "loading" ? (
        <section className="rounded-lg border border-dashed border-border p-6 text-sm text-muted-foreground">
          Loading billing details…
        </section>
      ) : view.status === "error" ? (
        <div
          role="alert"
          className="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive"
        >
          {view.message}
          <div className="mt-3">
            <Button variant="outline" size="sm" onClick={() => void load()}>
              Retry
            </Button>
          </div>
        </div>
      ) : (
        <>
          <section
            aria-label="Account"
            className="rounded-lg border border-border bg-card p-4 text-sm text-card-foreground"
          >
            Signed in as{" "}
            <span className="font-medium">{view.user.email}</span>
          </section>

          <section aria-labelledby="balance-heading" className="flex flex-col gap-4">
            <div>
              <h2
                id="balance-heading"
                className="text-lg font-semibold"
              >
                Credit balance
              </h2>
              <p className="text-sm text-muted-foreground">
                All amounts are integer microcredits. One credit is exactly
                1,000,000 microcredits.
              </p>
            </div>
            <dl className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              <Metric label="Balance" microcredits={view.balance.balance_microcredits} />
              <Metric
                label="Reserved"
                microcredits={view.balance.reserved_microcredits}
              />
              <Metric
                label="Available"
                microcredits={view.balance.available_microcredits}
              />
              <Metric
                label="Lifetime purchased"
                microcredits={view.balance.lifetime_purchased_microcredits}
              />
              <Metric
                label="Lifetime spent"
                microcredits={view.balance.lifetime_spent_microcredits}
              />
            </dl>
          </section>

          <section aria-labelledby="packs-heading" className="flex flex-col gap-4">
            <div>
              <h2 id="packs-heading" className="text-lg font-semibold">
                Buy credit packs
              </h2>
              <p className="text-sm text-muted-foreground">
                Checkout is hosted by Stripe. Credits are added only after the
                verified webhook is processed.
              </p>
            </div>
            {checkoutNotice ? (
              <p
                role="alert"
                className="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive"
              >
                {checkoutNotice.message}
              </p>
            ) : null}
            <div className="grid gap-4 md:grid-cols-3">
              {CREDIT_PACKS.map((pack) => (
                <article
                  key={pack.id}
                  data-pack-id={pack.id}
                  data-selected={String(selectedPackId === pack.id)}
                  className={`flex flex-col gap-3 rounded-lg border bg-card p-5 text-card-foreground ${
                    selectedPackId === pack.id
                      ? "border-primary"
                      : "border-border"
                  }`}
                >
                  <h3 className="text-2xl font-semibold tabular-nums">
                    {pack.price}
                  </h3>
                  <p className="text-sm font-medium">{pack.credits} credits</p>
                  <p className="font-mono text-xs text-muted-foreground">
                    {pack.microcredits} microcredits
                  </p>
                  <Button
                    type="button"
                    className="mt-auto"
                    disabled={pendingPackId !== null}
                    onClick={() => void handleBuy(pack)}
                  >
                    {pendingPackId === pack.id
                      ? "Starting checkout…"
                      : "Buy credits"}
                  </Button>
                </article>
              ))}
            </div>
          </section>

          <section aria-labelledby="ledger-heading" className="flex flex-col gap-4">
            <div>
              <h2 id="ledger-heading" className="text-lg font-semibold">
                Recent credit activity
              </h2>
              <p className="text-sm text-muted-foreground">
                The latest {LEDGER_PAGE_SIZE} ledger entries, newest first.
              </p>
            </div>
            <div className="overflow-x-auto rounded-lg border border-border bg-card text-card-foreground">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-border text-xs uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <th scope="col" className="px-4 py-3 font-medium">
                      Created
                    </th>
                    <th scope="col" className="px-4 py-3 font-medium">
                      Type
                    </th>
                    <th scope="col" className="px-4 py-3 font-medium">
                      Amount (microcredits)
                    </th>
                    <th scope="col" className="px-4 py-3 font-medium">
                      Reference ID
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {view.ledger.length === 0 ? (
                    <tr>
                      <td
                        colSpan={4}
                        className="px-4 py-6 text-center text-muted-foreground"
                      >
                        No credit activity yet.
                      </td>
                    </tr>
                  ) : (
                    view.ledger.map((entry) => (
                      <tr key={entry.id}>
                        <td className="whitespace-nowrap px-4 py-3 text-muted-foreground">
                          {formatDate(entry.created_at)}
                        </td>
                        <td className="px-4 py-3 font-mono text-xs">
                          {entry.type}
                        </td>
                        <td className="px-4 py-3 font-mono text-xs tabular-nums">
                          {signedAmount(entry.amount_microcredits)}
                        </td>
                        <td className="px-4 py-3 font-mono text-xs text-muted-foreground">
                          {entry.reference_id ?? "—"}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </>
  );
}

function Metric({
  label,
  microcredits,
}: {
  label: string;
  microcredits: number;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-5 text-card-foreground">
      <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className="mt-2 text-2xl font-semibold tabular-nums">
        {String(microcredits)}{" "}
        <span className="text-xs font-normal text-muted-foreground">
          microcredits
        </span>
      </dd>
      <dd className="mt-1 text-xs text-muted-foreground">
        {formatCredits(microcredits)} credits
      </dd>
    </div>
  );
}

/** Show whole credits plus exact sub-credit digits using integer math only. */
function formatCredits(microcredits: number): string {
  if (!Number.isSafeInteger(microcredits)) return String(microcredits);
  const value = BigInt(microcredits);
  const whole = value / MICROCREDITS_PER_CREDIT;
  const fraction = value % MICROCREDITS_PER_CREDIT;
  if (fraction === 0n) return whole.toString();
  const digits = fraction
    .toString()
    .padStart(6, "0")
    .replace(/0+$/, "");
  return `${whole.toString()}.${digits}`;
}

function signedAmount(microcredits: number): string {
  return microcredits > 0 ? `+${microcredits}` : String(microcredits);
}

function isSafeRedirect(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:";
  } catch {
    return false;
  }
}

function formatDate(value: string): string {
  try {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(value));
  } catch {
    return value;
  }
}
