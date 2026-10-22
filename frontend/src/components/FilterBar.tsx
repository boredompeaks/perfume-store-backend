"use client";

import { useRouter, useSearchParams } from "next/navigation";
import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import {
  DEFAULT_ORDERING,
  isInvalidPrice,
  type Ordering,
} from "@/lib/filters";
import { inputClass } from "@/lib/ui";

/**
 * Real backend orderings only (lib/filters.ts ORDERINGS) — the screen's
 * "Formulation density" option has no API behind it and is not shipped (P5).
 * The first three labels follow the screen; name order is the API's own extra.
 */
const sortOptions: { value: Ordering; label: string }[] = [
  { value: "-created_at", label: "Chronological release" },
  { value: "price", label: "Price: Low to high" },
  { value: "-price", label: "Price: High to low" },
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
 * Sort control (U-W11-T5). The screen renders a native `<select>`; xias
 * AGENTS.md is a non-negotiable against those ("Never a native `<select>`… Use
 * a button trigger with an ARIA listbox or combobox"), so the screen's VISUAL
 * treatment (label + field + chevron via inner padding) is rebuilt as the
 * select-only combobox pattern: the trigger shows the current value, the panel
 * is `role="listbox"` with `role="option"` rows, focus is a roving `-1` moved
 * onto the active option, and the full keyboard set (arrows, Home/End,
 * Enter/Space, Escape, outside-pointer dismiss) is wired here. Selection still
 * commits through the caller's `update()`, so the ordering param behaviour is
 * untouched.
 */
function SortControl({
  ordering,
  onSelect,
}: {
  ordering: Ordering;
  onSelect: (value: Ordering) => void;
}) {
  const [open, setOpen] = useState(false);
  const selectedIndex = Math.max(
    0,
    sortOptions.findIndex((option) => option.value === ordering),
  );
  const [activeIndex, setActiveIndex] = useState(selectedIndex);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const optionRefs = useRef<Array<HTMLLIElement | null>>([]);
  const selected = sortOptions[selectedIndex] ?? sortOptions[0];
  const listId = "catalog-sort-listbox";

  // Roving focus: the active option owns DOM focus while the panel is open.
  useEffect(() => {
    if (open) optionRefs.current[activeIndex]?.focus();
  }, [open, activeIndex]);

  // Dismiss on a pointer-down outside the control; focus stays where it was
  // (the pointer already expressed intent — yanking focus would surprise).
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      if (triggerRef.current?.contains(target)) return;
      if (listRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  const close = (restoreFocus: boolean) => {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  };

  const commit = (index: number) => {
    const option = sortOptions[index];
    if (!option) return;
    onSelect(option.value);
    close(true);
  };

  const onTriggerKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    setActiveIndex(
      event.key === "ArrowDown"
        ? selectedIndex
        : (selectedIndex - 1 + sortOptions.length) % sortOptions.length,
    );
    setOpen(true);
  };

  const onListKeyDown = (event: ReactKeyboardEvent<HTMLUListElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close(true);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((activeIndex + 1) % sortOptions.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((activeIndex - 1 + sortOptions.length) % sortOptions.length);
    } else if (event.key === "Home") {
      event.preventDefault();
      setActiveIndex(0);
    } else if (event.key === "End") {
      event.preventDefault();
      setActiveIndex(sortOptions.length - 1);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      commit(activeIndex);
    } else if (event.key === "Tab") {
      setOpen(false);
    }
  };

  return (
    <div className="flex shrink-0 items-center gap-2">
      <span
        id="sort-label"
        className="whitespace-nowrap text-eyebrow uppercase text-ink-secondary"
      >
        Sort by
      </span>
      <div className="relative">
        <button
          ref={triggerRef}
          id="sort-button"
          type="button"
          aria-labelledby="sort-label sort-button"
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={listId}
          onClick={() => {
            setActiveIndex(selectedIndex);
            setOpen(!open);
          }}
          onKeyDown={onTriggerKeyDown}
          className={`${inputClass} flex cursor-pointer items-center pr-9 text-left`}
        >
          {selected.label}
          {/* Chevron drawn with currentColor, placed by the trigger's inner
              padding (AGENTS.md) — never the select's UA widget. */}
          <svg
            aria-hidden="true"
            focusable="false"
            viewBox="0 0 16 16"
            className="pointer-events-none absolute right-3 top-1/2 size-4 -translate-y-1/2 text-ink-secondary"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
          >
            <path d="M4 6l4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
        {open && (
          <ul
            ref={listRef}
            id={listId}
            role="listbox"
            aria-labelledby="sort-label"
            onKeyDown={onListKeyDown}
            className="absolute right-0 top-full z-(--z-sticky) mt-1 min-w-full border border-field-border bg-surface py-1"
          >
            {sortOptions.map((option, index) => (
              <li
                key={option.value}
                ref={(node) => {
                  optionRefs.current[index] = node;
                }}
                role="option"
                aria-selected={option.value === ordering}
                tabIndex={-1}
                onClick={() => commit(index)}
                onMouseEnter={() => setActiveIndex(index)}
                className={`min-h-10 cursor-pointer px-3 py-2 text-sm hover:bg-hairline ${
                  option.value === ordering ? "bg-hairline font-medium" : ""
                }`}
              >
                {option.label}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/**
 * Client filter bar writing to the URL (shareable, noindex when narrowed).
 * Search is debounced 400ms; price filters apply on Apply; any change resets
 * the page param. Category renders as a removable chip only — the backend has
 * no categories endpoint (BACKEND_REQUESTS), so there is no picker to render.
 * Layout follows the /products screen (U-W11-T5): a hairline rule above, then
 * search left, min/max + Apply middle, sort right; the row wraps at xs.
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
    <div className="mt-8 border-t border-hairline pt-4">
      {/* Screen arrangement (U-W11-T5): one row at md+ — search left, price +
          Apply middle, sort right — wrapping at xs where the search takes its
          own full-width row. */}
      <div className="flex flex-wrap items-center gap-3 md:flex-nowrap">
        <div className="w-full min-w-0 md:w-80 lg:w-96">
          <label htmlFor="product-search" className="sr-only">
            Search fragrances
          </label>
          <input
            id="product-search"
            type="search"
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            placeholder="Search formulations or notes…"
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
            placeholder="₹ Min"
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
            placeholder="₹ Max"
            className={`${inputClass} w-24`}
          />
          <button
            type="button"
            onClick={applyPrices}
            className="border border-ink px-4 py-2.5 text-eyebrow uppercase transition-colors hover:bg-ink hover:text-paper"
          >
            Apply
          </button>
        </div>

        <div className="ml-auto">
          <SortControl
            ordering={ordering}
            onSelect={(value) =>
              update({
                ordering: value === DEFAULT_ORDERING ? null : value,
              })
            }
          />
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
