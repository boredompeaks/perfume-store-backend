"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import {
  DEFAULT_ORDERING,
  isInvalidPrice,
  type Ordering,
} from "@/lib/filters";

const sortOptions: { value: Ordering; label: string }[] = [
  { value: "-created_at", label: "Newest" },
  { value: "price", label: "Price: low to high" },
  { value: "-price", label: "Price: high to low" },
  { value: "name", label: "Name A–Z" },
  { value: "-name", label: "Name Z–A" },
];

const inputClass =
  "border border-line bg-surface px-3 py-2 text-sm placeholder:text-ink-muted/70 focus:outline-none";

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
    <div className="mt-8 border-b border-line pb-5">
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <label htmlFor="product-search" className="sr-only">
            Search fragrances
          </label>
          <input
            id="product-search"
            type="search"
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            placeholder="Search fragrances"
            className={`${inputClass} w-56`}
          />
        </div>

        <div className="flex items-center gap-2">
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

        <div className="ml-auto flex items-center gap-2">
          <label htmlFor="sort" className="text-sm text-ink-muted">
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

      {priceError && (
        <p role="alert" className="mt-2 text-sm text-bronze">
          {priceError}
        </p>
      )}

      {category && (
        <div className="mt-3 flex items-center gap-2 text-sm">
          <span className="text-ink-muted">Category:</span>
          <button
            type="button"
            onClick={() => update({ category: null })}
            className="flex items-center gap-1.5 border border-line px-2.5 py-1 transition-colors hover:border-ink"
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
