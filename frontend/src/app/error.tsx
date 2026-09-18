"use client";

export default function ErrorPage({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <section className="mx-auto max-w-6xl px-4 py-32 text-center sm:px-6">
      <p className="text-xs uppercase tracking-[0.2em] text-bronze">
        Something went wrong
      </p>
      <h1 className="mt-4 font-display text-4xl tracking-tight">
        We couldn&apos;t load this page.
      </h1>
      <p className="mx-auto mt-4 max-w-md text-ink-muted">
        The request failed. Retry, or come back in a moment.
      </p>
      <button
        type="button"
        onClick={reset}
        className="mt-8 bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
      >
        Try again
      </button>
    </section>
  );
}
