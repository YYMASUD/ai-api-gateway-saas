"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { ApiError, type AuthUser, createApiClient } from "@/lib/api-client";
import { clearAccessToken, getAccessToken } from "@/lib/auth-token";

type AccountState =
  | { status: "loading" }
  | { status: "signed-out" }
  | { status: "ready"; user: AuthUser }
  | { status: "error"; message: string };

/**
 * Small account summary for dashboard surfaces. Shows email/role from
 * `GET /auth/me` and a sign-out button that clears the session token.
 * The token itself is never rendered.
 */
export function AccountCard() {
  const [state, setState] = useState<AccountState>({ status: "loading" });

  const load = useCallback(async () => {
    const token = getAccessToken();
    if (!token) {
      setState({ status: "signed-out" });
      return;
    }
    setState({ status: "loading" });
    try {
      const user = await createApiClient({ token }).me();
      setState({ status: "ready", user });
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) {
        clearAccessToken();
        setState({ status: "signed-out" });
        return;
      }
      setState({
        status: "error",
        message:
          cause instanceof ApiError
            ? cause.message
            : "Unable to load account details.",
      });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function handleSignOut() {
    clearAccessToken();
    setState({ status: "signed-out" });
  }

  return (
    <section
      aria-label="Account"
      className="rounded-lg border border-border bg-card p-5 text-card-foreground"
    >
      <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
        Account
      </h2>
      {state.status === "loading" ? (
        <p className="mt-2 text-sm text-muted-foreground">Loading account…</p>
      ) : state.status === "signed-out" ? (
        <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-muted-foreground">
            Not signed in. Sign in to manage keys and usage.
          </p>
          <Button asChild variant="outline" size="sm">
            <Link href="/login">Sign in</Link>
          </Button>
        </div>
      ) : state.status === "error" ? (
        <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
          <p role="alert" className="text-sm text-destructive">
            {state.message}
          </p>
          <Button variant="outline" size="sm" onClick={() => void load()}>
            Retry
          </Button>
        </div>
      ) : (
        <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm">
            <span className="font-medium">{state.user.email}</span>
            <span className="ml-2 rounded-full border border-border px-2 py-0.5 text-xs uppercase tracking-wide text-muted-foreground">
              {state.user.role}
            </span>
          </p>
          <Button variant="outline" size="sm" onClick={handleSignOut}>
            Sign out
          </Button>
        </div>
      )}
    </section>
  );
}
