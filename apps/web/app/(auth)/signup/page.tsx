import type { Metadata } from "next";

import {
  AuthCard,
  AuthFooterLink,
  AuthPlaceholderNotice,
} from "@/components/auth-card";
import { Button } from "@/components/ui/button";

export const metadata: Metadata = { title: "Create account" };

export default function SignupPage() {
  return (
    <AuthCard
      title="Create your account"
      description="Start routing AI traffic through a single audited gateway."
      footer={<AuthFooterLink prompt="Already registered?" href="/login" label="Sign in" />}
    >
      <form className="flex flex-col gap-4" aria-label="Sign up form">
        <div className="flex flex-col gap-2">
          <label htmlFor="name" className="text-sm font-medium">
            Name
          </label>
          <input
            id="name"
            name="name"
            type="text"
            autoComplete="name"
            required
            placeholder="Ada Lovelace"
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
          />
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
            placeholder="you@company.com"
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
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
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
          />
          <p className="text-xs text-muted-foreground">
            Minimum 12 characters. Passwords are never stored in the browser
            bundle.
          </p>
        </div>
        <Button type="submit" className="w-full">
          Create account
        </Button>
        <AuthPlaceholderNotice />
      </form>
    </AuthCard>
  );
}
