import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "AI API Gateway",
    template: "%s | AI API Gateway",
  },
  description:
    "Production-grade gateway for unified, observable access to AI model APIs.",
  robots: {
    index: false,
    follow: false,
  },
};

const navLinks = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/docs", label: "Docs" },
  { href: "/admin", label: "Admin" },
];

const apiBaseUrl =
  process.env.NEXT_PUBLIC_API_BASE_URL?.trim() || "http://localhost:8000";

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="flex min-h-screen flex-col bg-background font-sans">
        <header className="border-b border-border/70">
          <nav
            className="container flex h-16 items-center justify-between"
            aria-label="Main navigation"
          >
            <Link href="/" className="font-semibold tracking-tight">
              AI API Gateway
            </Link>
            <div className="flex items-center gap-5 text-sm text-muted-foreground">
              {navLinks.map((link) => (
                <Link
                  key={link.href}
                  href={link.href}
                  className="transition-colors hover:text-foreground"
                >
                  {link.label}
                </Link>
              ))}
              <Link
                href="/login"
                className="rounded-md bg-primary px-3 py-2 font-medium text-primary-foreground transition-colors hover:bg-primary/90"
              >
                Sign in
              </Link>
            </div>
          </nav>
        </header>
        <main className="container flex-1 py-10">{children}</main>
        <footer className="border-t border-border/70 py-6">
          <p className="container text-sm text-muted-foreground">
            Phase 1 scaffold — gateway base URL at {apiBaseUrl}
          </p>
        </footer>
      </body>
    </html>
  );
}
