import type { Metadata } from "next";
import type { ReactNode } from "react";

// The page itself is a client component (working form), so its metadata lives
// in this server layout instead.
export const metadata: Metadata = { title: "Create account" };

export default function SignupLayout({ children }: { children: ReactNode }) {
  return children;
}
