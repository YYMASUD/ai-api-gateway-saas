import type { Metadata } from "next";

import {
  AuthCard,
  AuthFooterLink,
  AuthPlaceholderNotice,
} from "@/components/auth-card";
import { Button } from "@/components/ui/button";

export const metadata: Metadata = { title: "Reset password" };

export default function ResetPage() {
  return (
    <AuthCard
      title="Reset your password"
      description="We will email a single-use reset link if the address matches an account."
      footer={<AuthFooterLink prompt="Remembered it?" href="/login" label="Back to sign in" />}
    >
      <form className="flex flex-col gap-4" aria-label="Password reset form">
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
        <Button type="submit" className="w-full">
          Send reset link
        </Button>
        <AuthPlaceholderNotice />
      </form>
    </AuthCard>
  );
}
