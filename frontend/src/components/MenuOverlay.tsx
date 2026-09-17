"use client";

import Link from "next/link";
import { useEffect, useRef } from "react";
import { useAuth } from "@/lib/auth";
import { site } from "@/lib/site";

const links = [
  { href: "/", label: "Home" },
  { href: "/products", label: "Shop all" },
  { href: "/products?ordering=-created_at", label: "New arrivals" },
  { href: "/cart", label: "Cart" },
  { href: "/orders", label: "Orders" },
];

export default function MenuOverlay({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const { status, logout } = useAuth();
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const panel = panelRef.current;
    if (!panel) return;

    const previouslyFocused = document.activeElement as HTMLElement | null;
    const focusables = () =>
      Array.from(
        panel.querySelectorAll<HTMLElement>("a[href], button:not([disabled])"),
      );
    focusables()[0]?.focus();

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const items = focusables();
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = "";
      previouslyFocused?.focus();
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      ref={panelRef}
      id="site-menu"
      role="dialog"
      aria-modal="true"
      aria-label="Menu"
      className="fixed inset-0 z-50 flex flex-col bg-paper"
    >
      <div className="flex h-16 items-center justify-between border-b border-line px-4 sm:px-6">
        <span className="font-display text-xl tracking-tight">
          {site.name}
        </span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close menu"
          className="-mr-2 p-2"
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
            <path d="M4 4l12 12M16 4L4 16" />
          </svg>
        </button>
      </div>

      <nav aria-label="Mobile" className="flex-1 px-6 pt-10">
        <ul className="flex flex-col">
          {links.map((link) => (
            <li key={link.label}>
              <Link
                href={link.href}
                onClick={onClose}
                className="block py-3 font-display text-3xl tracking-tight transition-colors hover:text-bronze"
              >
                {link.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>

      <div className="border-t border-line px-6 py-6 text-sm">
        {status === "authenticated" ? (
          <button
            type="button"
            onClick={() => {
              logout();
              onClose();
            }}
            className="text-ink-muted transition-colors hover:text-bronze"
          >
            Sign out
          </button>
        ) : status === "anonymous" ? (
          <Link
            href="/login"
            onClick={onClose}
            className="transition-colors hover:text-bronze"
          >
            Sign in
          </Link>
        ) : null}
      </div>
    </div>
  );
}
