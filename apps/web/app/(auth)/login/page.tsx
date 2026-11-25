import type { Metadata } from "next";
import Link from "next/link";

import {
  AuthCard,
  AuthFooterLink,
  AuthPlaceholderNotice,
} from "@/components/auth-card";
import { Button } from "@/components/ui/button";

export const metadata: Metadata = { title: "Sign in" };

export default function LoginPage() {
  return (
    <AuthCard
      title="Welcome back"
      description="Sign in to manage your gateway keys and usage."
      footer={<AuthFooterLink prompt="New here?" href="/signup" label="Create an account" />}
    >
      <form className="flex flex-col gap-4" aria-label="Sign in form">
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
            placeholder="you@example.com"
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
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
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
          />
        </div>
        <Button type="submit" className="w-full">
          Sign in
        </Button>
        <AuthPlaceholderNotice />
      </form>
    </AuthCard>
  );
}
