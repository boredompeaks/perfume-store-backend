"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import { cartCount, getCart } from "@/lib/cart-api";
import { site } from "@/lib/site";
import MenuOverlay from "./MenuOverlay";

const navLinks = [
  { href: "/products", label: "Shop" },
  { href: "/products?ordering=-created_at", label: "New arrivals" },
];

function CartIcon() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 20 20"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
    >
      <path d="M4.5 6.5h11l-.8 9a1.5 1.5 0 0 1-1.5 1.4H6.8a1.5 1.5 0 0 1-1.5-1.4l-.8-9Z" />
      <path d="M7 6.5V6a3 3 0 0 1 6 0v.5" />
    </svg>
  );
}

export default function Header() {
  const pathname = usePathname();
  const { status, logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);

  const { data: cart } = useQuery({
    queryKey: ["cart"],
    queryFn: getCart,
    staleTime: 15_000,
    retry: false,
  });
  const count = cartCount(cart);

  // Close the overlay on any navigation.
  useEffect(() => {
    setMenuOpen(false);
  }, [pathname]);

  return (
    <>
      <header className="sticky top-0 z-40 border-b border-line bg-paper">
        <div className="relative mx-auto flex h-16 max-w-6xl items-center justify-between px-4 sm:px-6">
          <div className="flex items-center">
            <button
              type="button"
              aria-expanded={menuOpen}
              aria-controls="site-menu"
              aria-label="Open menu"
              onClick={() => setMenuOpen(true)}
              className="-ml-2 p-2 md:hidden"
            >
              <svg
                width="20"
                height="20"
                viewBox="0 0 20 20"
                aria-hidden="true"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
              >
                <path d="M2 5h16M2 10h16M2 15h16" />
              </svg>
            </button>
            <nav
              aria-label="Primary"
              className="hidden items-center gap-7 md:flex"
            >
              {navLinks.map((link) => (
                <Link
                  key={link.label}
                  href={link.href}
                  className="text-sm transition-colors hover:text-bronze"
                >
                  {link.label}
                </Link>
              ))}
            </nav>
          </div>

          <Link
            href="/"
            className="absolute left-1/2 -translate-x-1/2 font-display text-xl tracking-tight"
          >
            {site.name}
          </Link>

          <div className="flex items-center gap-4 text-sm">
            {status === "authenticated" ? (
              <>
                <Link
                  href="/orders"
                  className="hidden transition-colors hover:text-bronze sm:inline"
                >
                  Orders
                </Link>
                <button
                  type="button"
                  onClick={logout}
                  className="hidden text-ink-muted transition-colors hover:text-bronze sm:inline"
                >
                  Sign out
                </button>
              </>
            ) : status === "anonymous" ? (
              <Link
                href="/login"
                className="hidden transition-colors hover:text-bronze sm:inline"
              >
                Sign in
              </Link>
            ) : (
              <span className="hidden w-12 sm:inline" aria-hidden="true" />
            )}

            <Link
              href="/cart"
              className="relative flex items-center p-1 transition-colors hover:text-bronze"
              aria-label={`Cart, ${count} item${count === 1 ? "" : "s"}`}
            >
              <CartIcon />
              <span
                aria-live="polite"
                className={
                  count > 0
                    ? "absolute -right-2 -top-1 min-w-[1.1rem] rounded-full bg-ink px-1 text-center text-[11px] leading-5 text-paper"
                    : "sr-only"
                }
              >
                {count > 0 ? count : "0 items"}
              </span>
            </Link>
          </div>
        </div>
      </header>

      <MenuOverlay open={menuOpen} onClose={() => setMenuOpen(false)} />
    </>
  );
}
