"use client";

import Link from "next/link";
import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import {
  type ApiKey,
  type AuthUser,
  type CreatedApiKey,
  createApiClient,
  isSessionInvalidError,
} from "@/lib/api-client";
import { clearAccessToken, getAccessToken } from "@/lib/auth-token";

const AVAILABLE_SCOPES = ["chat:completions", "models:read"] as const;
const DEFAULT_SCOPES: string[] = [...AVAILABLE_SCOPES];

interface ViewState {
  status: "idle" | "loading" | "ready" | "unauthenticated" | "error";
  user: AuthUser | null;
  keys: ApiKey[];
  error: string | null;
}

/**
 * Phase 2 key management. The one-time plaintext key returned by
 * `POST /dashboard/keys` lives only in React state: it is never written to
 * storage, so leaving or reloading this page removes it from the DOM.
 */
export default function KeysPage() {
  const [view, setView] = useState<ViewState>({
    status: "idle",
    user: null,
    keys: [],
    error: null,
  });
  const [newKeyName, setNewKeyName] = useState("");
  const [selectedScopes, setSelectedScopes] = useState<string[]>(DEFAULT_SCOPES);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [freshKey, setFreshKey] = useState<CreatedApiKey | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [revokeError, setRevokeError] = useState<string | null>(null);

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

  const signedOut = useCallback(() => {
    setFreshKey(null);
    setView({ status: "unauthenticated", user: null, keys: [], error: null });
  }, []);

  /**
   * Handle a 401/403 for a request bound to `capturedToken`. Only clear when
   * the stored token is still that token, so an old request's failure cannot
   * sign out a newer session.
   */
  const handleSessionFailure = useCallback(
    (capturedToken: string) => {
      if (getAccessToken() !== capturedToken) return;
      clearAccessToken();
      if (!mountedRef.current) return;
      signedOut();
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

    setView((prev) => ({ ...prev, status: "loading", error: null }));
    try {
      const client = createApiClient({ token });
      const [user, keys] = await Promise.all([client.me(), client.listKeys()]);
      if (!mountedRef.current || loadIdRef.current !== loadId) return;
      setView({ status: "ready", user, keys, error: null });
    } catch (cause) {
      if (!mountedRef.current || loadIdRef.current !== loadId) return;
      if (isSessionInvalidError(cause)) {
        handleSessionFailure(token);
        return;
      }
      setView((prev) => ({
        ...prev,
        status: "error",
        error:
          cause instanceof Error && cause.message
            ? cause.message
            : "Unable to load API keys right now.",
      }));
    }
  }, [handleSessionFailure, signedOut]);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const token = getAccessToken();
    if (!token) {
      signedOut();
      return;
    }
    // Never omit scopes: the API would fall back to a fully-scoped key.
    if (selectedScopes.length === 0) {
      setCreateError("Select at least one scope.");
      return;
    }

    setCreating(true);
    setCreateError(null);
    setFreshKey(null);
    setCopyState("idle");
    try {
      const client = createApiClient({ token });
      const created = await client.createKey({
        name: newKeyName.trim(),
        scopes: selectedScopes,
      });
      if (!mountedRef.current) return;
      if (getAccessToken() !== token) return;
      setFreshKey(created);
      setNewKeyName("");
      await load();
    } catch (cause) {
      if (!mountedRef.current) return;
      if (isSessionInvalidError(cause)) {
        handleSessionFailure(token);
        return;
      }
      setCreateError(
        cause instanceof Error && cause.message
          ? cause.message
          : "Could not create key. Please try again.",
      );
    } finally {
      if (mountedRef.current) setCreating(false);
    }
  }

  async function handleRevoke(apiKey: ApiKey) {
    const confirmed =
      typeof window !== "undefined" &&
      window.confirm(
        `Revoke "${apiKey.name}"? Applications using it will stop working immediately.`,
      );
    if (!confirmed) return;

    const token = getAccessToken();
    if (!token) {
      signedOut();
      return;
    }

    setRevokingId(apiKey.id);
    setRevokeError(null);
    try {
      const client = createApiClient({ token });
      await client.revokeKey(apiKey.id);
      if (!mountedRef.current || getAccessToken() !== token) return;
      await load();
    } catch (cause) {
      if (!mountedRef.current) return;
      if (isSessionInvalidError(cause)) {
        handleSessionFailure(token);
        return;
      }
      setRevokeError(
        cause instanceof Error && cause.message
          ? cause.message
          : "Failed to revoke key. Please try again.",
      );
    } finally {
      if (mountedRef.current) setRevokingId(null);
    }
  }

  async function handleCopy() {
    if (!freshKey) return;
    try {
      await navigator.clipboard.writeText(freshKey.key);
      if (mountedRef.current) setCopyState("copied");
    } catch {
      if (mountedRef.current) setCopyState("failed");
    }
  }

  function dismissFreshKey() {
    setFreshKey(null);
    setCopyState("idle");
  }

  function toggleScope(scope: string) {
    setSelectedScopes((prev) =>
      prev.includes(scope)
        ? prev.filter((item) => item !== scope)
        : [...prev, scope],
    );
  }

  if (view.status === "unauthenticated") {
    return (
      <>
        <PageHeader
          title="API keys"
          description="Issue scoped gateway keys and revoke them without downtime."
        />
        <section className="rounded-lg border border-border bg-card p-8 text-center text-card-foreground">
          <h2 className="text-lg font-semibold">Sign in to manage keys</h2>
          <p className="mt-2 text-sm text-muted-foreground">
            Authentication is required before you can create, list, or revoke
            gateway keys.
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

  const scopesMissing = selectedScopes.length === 0;

  return (
    <>
      <PageHeader
        title="API keys"
        description="Issue scoped gateway keys and revoke them without downtime."
      />

      {view.user ? (
        <section className="rounded-lg border border-border bg-card p-4 text-sm text-card-foreground">
          <p>
            Signed in as{" "}
            <span className="font-medium">{view.user.email}</span>
            <span className="ml-2 rounded-full border border-border px-2 py-0.5 text-xs uppercase tracking-wide text-muted-foreground">
              {view.user.role}
            </span>
          </p>
        </section>
      ) : null}

      {freshKey ? (
        <div
          role="status"
          className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-4 text-sm"
        >
          <p className="font-semibold">Key created successfully</p>
          <p className="mt-1 text-muted-foreground">
            Copy this key now. It will not be shown again after you leave or
            reload this page.
          </p>
          <pre className="mt-3 overflow-x-auto whitespace-pre-wrap break-all rounded-md border border-emerald-500/30 bg-background p-3 font-mono text-xs">
            {freshKey.key}
          </pre>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <Button type="button" size="sm" onClick={() => void handleCopy()}>
              {copyState === "copied" ? "Copied!" : "Copy key"}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={dismissFreshKey}
            >
              Done — I saved this key
            </Button>
            {copyState === "failed" ? (
              <span className="text-xs text-muted-foreground">
                Clipboard unavailable — select the key above and copy it
                manually.
              </span>
            ) : null}
          </div>
        </div>
      ) : null}

      {view.status === "idle" || view.status === "loading" ? (
        <div className="rounded-lg border border-dashed border-border p-6 text-sm text-muted-foreground">
          Loading keys…
        </div>
      ) : view.status === "error" ? (
        <div
          role="alert"
          className="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive"
        >
          {view.error ?? "Unknown error"}
          <div className="mt-3">
            <Button variant="outline" size="sm" onClick={() => void load()}>
              Retry
            </Button>
          </div>
        </div>
      ) : (
        <section className="flex flex-col gap-4">
          <div className="rounded-lg border border-border bg-card p-5 text-card-foreground">
            <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
              Create a new key
            </h2>
            <form
              className="mt-4 flex flex-col gap-4"
              aria-label="Create API key form"
              onSubmit={handleCreate}
            >
              <div className="flex flex-col gap-2">
                <label htmlFor="key-name" className="text-sm font-medium">
                  Key name
                </label>
                <input
                  id="key-name"
                  name="key-name"
                  type="text"
                  required
                  maxLength={64}
                  value={newKeyName}
                  onChange={(event) => setNewKeyName(event.target.value)}
                  placeholder="Production backend"
                  className="h-10 rounded-md border border-input bg-background px-3 text-sm"
                  disabled={creating}
                />
                <p className="text-xs text-muted-foreground">
                  Up to 64 characters.
                </p>
              </div>
              <fieldset className="flex flex-col gap-2">
                <legend className="text-sm font-medium">Scopes</legend>
                {AVAILABLE_SCOPES.map((scope) => (
                  <label key={scope} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={selectedScopes.includes(scope)}
                      onChange={() => toggleScope(scope)}
                      disabled={creating}
                      className="size-4 rounded border-input"
                    />
                    <span className="font-mono text-xs">{scope}</span>
                  </label>
                ))}
                {scopesMissing ? (
                  <p className="text-xs text-destructive">
                    Select at least one scope.
                  </p>
                ) : null}
              </fieldset>
              {createError ? (
                <p role="alert" className="text-sm text-destructive">
                  {createError}
                </p>
              ) : null}
              <Button
                type="submit"
                disabled={creating || !newKeyName.trim() || scopesMissing}
              >
                {creating ? "Creating…" : "Create key"}
              </Button>
            </form>
          </div>

          <div className="rounded-lg border border-border bg-card text-card-foreground">
            <div className="border-b border-border px-5 py-4">
              <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
                Existing keys
              </h2>
            </div>
            {revokeError ? (
              <p
                role="alert"
                className="border-b border-border px-5 py-3 text-sm text-destructive"
              >
                {revokeError}
              </p>
            ) : null}
            {view.keys.length === 0 ? (
              <p className="p-5 text-sm text-muted-foreground">
                No keys yet. Create one above to start routing traffic.
              </p>
            ) : (
              <ul className="divide-y divide-border">
                {view.keys.map((apiKey) => (
                  <li
                    key={apiKey.id}
                    className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-start sm:justify-between"
                  >
                    <div className="flex flex-col gap-1">
                      <span className="font-medium">{apiKey.name}</span>
                      <code className="font-mono text-xs text-muted-foreground">
                        {apiKey.prefix}
                      </code>
                      {apiKey.scopes.length ? (
                        <div className="flex flex-wrap gap-1 text-xs text-muted-foreground">
                          {apiKey.scopes.map((scope) => (
                            <span
                              key={scope}
                              className="rounded-full border border-border px-2 py-0.5 font-mono"
                            >
                              {scope}
                            </span>
                          ))}
                        </div>
                      ) : null}
                      <div className="text-xs text-muted-foreground">
                        {apiKey.revoked_at
                          ? `Revoked ${formatDate(apiKey.revoked_at)}`
                          : apiKey.last_used_at
                            ? `Last used ${formatDate(apiKey.last_used_at)}`
                            : `Created ${formatDate(apiKey.created_at)}`}
                      </div>
                    </div>
                    {!apiKey.revoked_at ? (
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={() => void handleRevoke(apiKey)}
                        disabled={revokingId === apiKey.id}
                      >
                        {revokingId === apiKey.id ? "Revoking…" : "Revoke"}
                      </Button>
                    ) : (
                      <span className="inline-flex h-9 items-center rounded-md border border-border px-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Revoked
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>
      )}
    </>
  );
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
