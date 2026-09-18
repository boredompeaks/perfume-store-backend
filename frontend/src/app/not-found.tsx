import Link from "next/link";

export default function NotFound() {
  return (
    <section className="mx-auto max-w-6xl px-4 py-32 text-center sm:px-6">
      <p className="text-xs uppercase tracking-[0.2em] text-bronze">404</p>
      <h1 className="mt-4 font-display text-4xl tracking-tight">
        This page doesn&apos;t exist.
      </h1>
      <p className="mx-auto mt-4 max-w-md text-ink-muted">
        The link may be outdated, or the product may have been retired.
      </p>
      <Link
        href="/"
        className="mt-8 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
      >
        Back to the house
      </Link>
    </section>
  );
}
