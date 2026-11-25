"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { AuthCard, AuthFooterLink } from "@/components/auth-card";
import { Button } from "@/components/ui/button";
import { ApiError, createApiClient } from "@/lib/api-client";
import { clearAccessToken, setAccessToken } from "@/lib/auth-token";

export default function SignupPage() {
  const router = useRouter();
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Stale-response guards: only the newest submit may store a token or
  // navigate, and no async result may touch state after unmount.
  const submitIdRef = useRef(0);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const submitId = ++submitIdRef.current;
    setError(null);
    // A failed attempt must not leave a previous session's token alive.
    clearAccessToken();

    if (password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }

    setLoading(true);
    try {
      const client = createApiClient();
      const response = await client.register({
        email,
        password,
        display_name: displayName.trim() || undefined,
      });
      if (!mountedRef.current || submitIdRef.current !== submitId) return;
      // Never store an unvalidated token; that would redirect into a 401 loop.
      if (
        typeof response?.access_token !== "string" ||
        response.access_token.length === 0
      ) {
        setError("Registration failed. Please try again.");
        return;
      }
      setAccessToken(response.access_token);
      router.push("/dashboard/keys");
    } catch (cause) {
      if (!mountedRef.current || submitIdRef.current !== submitId) return;
      if (cause instanceof ApiError) {
        setError(cause.message || "Registration failed. Please try again.");
      } else {
        setError("Network error. Please check your connection and retry.");
      }
    } finally {
      if (mountedRef.current && submitIdRef.current === submitId) {
        setLoading(false);
      }
    }
  }

  return (
    <AuthCard
      title="Create your account"
      description="Start routing AI traffic through a single audited gateway."
      footer={<AuthFooterLink prompt="Already registered?" href="/login" label="Sign in" />}
    >
      <form
        className="flex flex-col gap-4"
        aria-label="Sign up form"
        onSubmit={handleSubmit}
      >
        {error ? (
          <div
            role="alert"
            className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          >
            {error}
          </div>
        ) : null}
        <div className="flex flex-col gap-2">
          <label htmlFor="name" className="text-sm font-medium">
            Display name
          </label>
          <input
            id="name"
            name="name"
            type="text"
            autoComplete="name"
            placeholder="Ada Lovelace"
            maxLength={255}
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
            disabled={loading}
          />
          <p className="text-xs text-muted-foreground">
            Optional. Up to 255 characters.
          </p>
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor="email" className="text-sm font-medium">
            Work email
          </label>
          <input
            id="email"
            name="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="you@company.com"
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
            disabled={loading}
          />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor="password" className="text-sm font-medium">
            Password
          </label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="new-password"
            required
            minLength={12}
            maxLength={128}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
            disabled={loading}
          />
          <p className="text-xs text-muted-foreground">
            12-128 characters. Passwords are never stored in the browser
            bundle.
          </p>
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor="confirm-password" className="text-sm font-medium">
            Confirm password
          </label>
          <input
            id="confirm-password"
            name="confirm-password"
            type="password"
            autoComplete="new-password"
            required
            minLength={12}
            maxLength={128}
            value={confirmPassword}
            onChange={(event) => setConfirmPassword(event.target.value)}
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
            disabled={loading}
          />
        </div>
        <Button type="submit" className="w-full" disabled={loading}>
          {loading ? "Creating account…" : "Create account"}
        </Button>
      </form>
    </AuthCard>
  );
}
