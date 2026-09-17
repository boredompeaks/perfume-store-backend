import Link from "next/link";
import { site } from "@/lib/site";

export default function Footer() {
  return (
    <footer className="border-t border-line">
      <div className="mx-auto grid max-w-6xl gap-10 px-4 py-14 sm:px-6 md:grid-cols-3">
        <div>
          <p className="font-display text-lg tracking-tight">{site.name}</p>
          <p className="mt-2 max-w-xs text-sm text-ink-muted">
            {site.tagline}.
          </p>
        </div>
        <nav aria-label="Footer" className="flex flex-col items-start gap-2 text-sm">
          <Link href="/products" className="transition-colors hover:text-bronze">
            Shop all
          </Link>
          <Link href="/cart" className="transition-colors hover:text-bronze">
            Cart
          </Link>
          <Link href="/orders" className="transition-colors hover:text-bronze">
            Orders
          </Link>
          <Link href="/login" className="transition-colors hover:text-bronze">
            Sign in
          </Link>
        </nav>
        <div className="text-sm text-ink-muted md:text-right">
          <p>Prices in INR · Payments via Razorpay</p>
          <p className="mt-2">
            © {new Date().getFullYear()} {site.name}
          </p>
        </div>
      </div>
    </footer>
  );
}
