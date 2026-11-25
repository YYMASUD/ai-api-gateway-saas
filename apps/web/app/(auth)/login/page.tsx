"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { AuthCard, AuthFooterLink } from "@/components/auth-card";
import { Button } from "@/components/ui/button";
import { ApiError, createApiClient } from "@/lib/api-client";
import { clearAccessToken, setAccessToken } from "@/lib/auth-token";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
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
    setLoading(true);

    try {
      const client = createApiClient();
      const response = await client.login({ email, password });
      if (!mountedRef.current || submitIdRef.current !== submitId) return;
      // Never store an unvalidated token; that would redirect into a 401 loop.
      if (
        typeof response?.access_token !== "string" ||
        response.access_token.length === 0
      ) {
        setError("Sign in failed. Please try again.");
        return;
      }
      setAccessToken(response.access_token);
      router.push("/dashboard/keys");
    } catch (cause) {
      if (!mountedRef.current || submitIdRef.current !== submitId) return;
      if (cause instanceof ApiError) {
        if (cause.status === 401) {
          setError("Invalid email or password. Please try again.");
        } else {
          setError(cause.message || "Sign in failed. Please try again.");
        }
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
      title="Welcome back"
      description="Sign in to manage your gateway keys and usage."
      footer={
        <AuthFooterLink prompt="New here?" href="/signup" label="Create an account" />
      }
    >
      <form
        className="flex flex-col gap-4"
        aria-label="Sign in form"
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
          <label htmlFor="email" className="text-sm font-medium">
            Email
          </label>
          <input
            id="email"
            name="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="you@example.com"
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
            disabled={loading}
          />
        </div>
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <label htmlFor="password" className="text-sm font-medium">
              Password
            </label>
            <Link href="/reset" className="text-xs text-primary hover:underline">
              Forgot password?
            </Link>
          </div>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
            disabled={loading}
          />
        </div>
        <Button type="submit" className="w-full" disabled={loading}>
          {loading ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </AuthCard>
  );
}
