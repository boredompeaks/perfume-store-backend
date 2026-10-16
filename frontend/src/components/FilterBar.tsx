"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  DEFAULT_ORDERING,
  isInvalidPrice,
  type Ordering,
} from "@/lib/filters";
import { inputClass } from "@/lib/ui";

const sortOptions: { value: Ordering; label: string }[] = [
  { value: "-created_at", label: "Newest" },
  { value: "price", label: "Price: low to high" },
  { value: "-price", label: "Price: high to low" },
  { value: "name", label: "Name A–Z" },
  { value: "-name", label: "Name Z–A" },
];

/**
 * §5.2 Motion: the result-count line cross-fades on `--motion-state` when a
 * filter resolves — and never on arrival. §3's motion table (the "Home - hero
 * eyebrow + headline + sub" row) calls that hero "the only entrance animation
 * on the site", and §3.3/STITCH adds "no element enters on scroll, no card
 * staggers" for exactly this page — so a count that faded on first paint would
 * be a second entrance, above the fold, while the LCP image is still loading
 * (§10.1: nothing above the fold animates in).
 *
 * The arrival baseline is a ref kept across re-renders: SSR and the hydration
 * render are always plain, and only a customer-caused `?search=`/price/sort
 * resolve (a client-side navigation, which re-renders this island in place)
 * flips the line into the fading wrapper. The `key` is the params string so
 * the same fade replays on every resolve — including the resolve that CLEARS
 * the filters, where the params return to the arrival value: a `changed` flag
 * (once left, always fading) covers that, where comparing against the arrival
 * string alone would drop the cross-fade on "Clear all filters". The flag only
 * ever flips false → true, so a speculative render React discards cannot
 * un-set it.
 *
 * The wrapper is keyed, never the arrival state, so when the Suspense child
 * streams in and re-renders this island with the SAME params, React reuses the
 * div and the 200ms fade is not restarted mid-flight.
 *
 * Exported from this module — not its own file — so the /products task stays
 * inside its five-file budget; this is already the page's client island and
 * home does not import it.
 */
export function CountFade({ children }: { children: ReactNode }) {
  const params = useSearchParams().toString();
  const arrival = useRef(params);
  const changed = useRef(false);
  if (params !== arrival.current) changed.current = true;
  if (!changed.current) return <>{children}</>;
  return (
    <div key={params} className="count-settle">
      {children}
    </div>
  );
}

/**
 * Client filter bar writing to the URL (shareable, noindex when narrowed).
 * Search is debounced 400ms; price filters apply on Apply; any change resets
 * the page param. Category renders as a removable chip only — the backend has
 * no categories endpoint (BACKEND_REQUESTS), so there is no picker to render.
 */
export default function FilterBar() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const urlSearch = searchParams.get("search") ?? "";
  const category = searchParams.get("category");

  const [term, setTerm] = useState(urlSearch);
  const [minPrice, setMinPrice] = useState(searchParams.get("min_price") ?? "");
  const [maxPrice, setMaxPrice] = useState(searchParams.get("max_price") ?? "");
  const [priceError, setPriceError] = useState<string | null>(null);

  const update = (patch: Record<string, string | null>) => {
    const params = new URLSearchParams(searchParams.toString());
    for (const [key, value] of Object.entries(patch)) {
      if (value === null || value === "") params.delete(key);
      else params.set(key, value);
    }
    params.delete("page");
    const qs = params.toString();
    router.push(qs ? `/products?${qs}` : "/products", { scroll: false });
  };

  // Debounced search — never fires on mount (term === urlSearch initially).
  useEffect(() => {
    if (term === urlSearch) return;
    const timer = setTimeout(() => update({ search: term }), 400);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [term]);

  const applyPrices = () => {
    if (isInvalidPrice(minPrice || undefined) || isInvalidPrice(maxPrice || undefined)) {
      setPriceError("Prices must be numbers.");
      return;
    }
    setPriceError(null);
    update({ min_price: minPrice || null, max_price: maxPrice || null });
  };

  const ordering = (searchParams.get("ordering") as Ordering) ?? DEFAULT_ORDERING;

  return (
    <div className="mt-8 border-b border-hairline pb-5">
      {/* §5.2: one row at md+ (search left, price + Apply middle, sort right),
          wrapping at xs where the search takes its own full-width row. */}
      <div className="flex flex-wrap items-center gap-3 md:flex-nowrap">
        <div className="w-full min-w-0 md:w-56">
          <label htmlFor="product-search" className="sr-only">
            Search fragrances
          </label>
          <input
            id="product-search"
            type="search"
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            placeholder="Search fragrances"
            className={`${inputClass} w-full`}
          />
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <label htmlFor="price-min" className="sr-only">
            Minimum price
          </label>
          <input
            id="price-min"
            type="text"
            inputMode="decimal"
            value={minPrice}
            onChange={(e) => setMinPrice(e.target.value)}
            placeholder="Min ₹"
            className={`${inputClass} w-24`}
          />
          <label htmlFor="price-max" className="sr-only">
            Maximum price
          </label>
          <input
            id="price-max"
            type="text"
            inputMode="decimal"
            value={maxPrice}
            onChange={(e) => setMaxPrice(e.target.value)}
            placeholder="Max ₹"
            className={`${inputClass} w-24`}
          />
          <button
            type="button"
            onClick={applyPrices}
            className="border border-ink px-3 py-2 text-sm transition-colors hover:bg-ink hover:text-paper"
          >
            Apply
          </button>
        </div>

        <div className="ml-auto flex shrink-0 items-center gap-2">
          <label htmlFor="sort" className="text-sm text-ink-secondary">
            Sort
          </label>
          <select
            id="sort"
            value={ordering}
            onChange={(e) =>
              update({
                ordering:
                  e.target.value === DEFAULT_ORDERING ? null : e.target.value,
              })
            }
            className={`${inputClass} cursor-pointer`}
          >
            {sortOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* §0 CHANGE-2: a failure is never the accent colour. */}
      {priceError && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {priceError}
        </p>
      )}

      {category && (
        <div className="mt-3 flex items-center gap-2 text-sm">
          <span className="text-ink-secondary">Category:</span>
          {/* §2.1.5 E2: the chip's border is its only boundary, and hairline
              is 1.23:1 — a control boundary is `--color-field-border`
              (3.90:1 on the paper this chip sits on; 4.10:1 is the
              on-surface figure and is not this backdrop). */}
          <button
            type="button"
            onClick={() => update({ category: null })}
            className="flex items-center gap-1.5 border border-field-border px-2.5 py-1 transition-colors hover:border-ink"
          >
            {category}
            <span aria-hidden="true">×</span>
            <span className="sr-only">Remove category filter</span>
          </button>
        </div>
      )}
    </div>
  );
}
