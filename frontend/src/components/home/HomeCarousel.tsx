"use client";

import { useId, useState, type KeyboardEvent } from "react";
import Link from "next/link";
import ProductImage from "@/components/ProductImage";
import { formatINR } from "@/lib/money";
import type { Product } from "@/lib/types";

/**
 * §5.1 §2 rebuilt against the D1 screen
 * (`media/designs/maison_ther_catalogue_accueil_carousel_edition/`): the
 * "New arrivals" editorial carousel. The spec text described a 4-up grid;
 * the screen ships one plate at a time with a counter, two square controls
 * and four progress bars, and design owns layout (owner D1).
 *
 * Data (P5): the server already fetched the newest products by
 * `ordering=-created_at` and hands them over as props — no client fetch, no
 * second products endpoint, no placeholder slide. Fewer than four results
 * render as themselves: the counter, the progress bars and the slide count
 * are all derived from `products.length`, so nothing ever claims a slide
 * that does not exist.
 *
 * Motion (P3, ui-motion): the slide change is the ONE thing that moves in
 * this component, and only because the customer asked for it — manual
 * advance only, no timer, no autoplay, no scroll listener, no effect at all.
 * The track transition reads the `track` intent tokens
 * (`--motion-track-dur` + `--motion-ease-track`, §2.4.1's carousel row)
 * behind Tailwind's `motion-safe:` variant, so `prefers-reduced-motion:
 * reduce` never sees a transition to shorten: the transform simply lands on
 * its new value. Colour-only state changes (controls, progress bars) ride
 * the `state` token, which §2.4 reduces to 0ms under reduce. Nothing here
 * is above the hero, so nothing here can delay LCP (§10).
 *
 * ARIA: the region/slide/counter pattern from `.claude/rules/components.md`
 * (states table + keyboard-first inclusive design): `role="region"` labelled
 * by the section heading, `aria-roledescription="carousel"`, each slide a
 * `role="group"` labelled "n of total", a polite live region announcing the
 * current slide, `aria-label="Previous slide"` / `"Next slide"` on the two
 * controls, `aria-current` on the active progress bar, and ArrowLeft /
 * ArrowRight while focus is inside the region. Inactive slides are
 * `aria-hidden` and their only focusable child is taken out of the tab
 * order, so a keyboard user can never land on a plate they cannot see.
 */
export default function HomeCarousel({ products }: { products: Product[] }) {
  const headingId = useId();
  const total = products.length;
  const [index, setIndex] = useState(0);

  // Manual advance only: clamped, never wrapped, never scheduled.
  const goTo = (next: number) => setIndex(Math.min(Math.max(next, 0), total - 1));

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      goTo(index - 1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      goTo(index + 1);
    }
  };

  const pad = (n: number) => String(n).padStart(2, "0");
  // Shared-component contract (P5): no slides, no carousel. The page omits
  // the section before it ever gets here; this guard keeps a future caller
  // from rendering a live region that reads "Slide 1 of 0".
  if (products.length === 0) return null;
  const current = products[index];

  return (
    <div
      role="region"
      aria-roledescription="carousel"
      aria-labelledby={headingId}
      onKeyDown={onKeyDown}
    >
      {/* Section head: screen's own row — eyebrow + h2 left, counter and the
          two square controls right, closed by a hairline (code.html:201). */}
      <div className="flex flex-wrap items-end justify-between gap-4 border-b border-hairline pb-6">
        <div>
          <p className="text-eyebrow uppercase text-bronze">Formulations 2024</p>
          <h2 id={headingId} className="mt-2 font-display text-h1">
            New arrivals
          </h2>
        </div>
        <div className="flex items-center gap-6 sm:gap-8">
          {/* The visible counter is decoration for AT (the live region below
              carries the same fact as prose), so it stays out of the reading
              order but keeps tabular figures so the swap cannot jitter. */}
          <p
            aria-hidden="true"
            className="select-none text-label-sm uppercase tabular-nums"
          >
            <span className="text-ink">{pad(index + 1)}</span>
            <span className="mx-1 text-ink-secondary">/</span>
            <span className="text-ink-secondary">{pad(total)}</span>
          </p>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => goTo(index - 1)}
              disabled={index === 0}
              aria-label="Previous slide"
              className="flex h-11 min-w-11 items-center justify-center border border-ink text-ink transition-colors duration-(--motion-state-dur) hover:bg-ink hover:text-paper disabled:pointer-events-none disabled:opacity-30 lg:h-10 lg:min-w-10"
            >
              <svg
                aria-hidden="true"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                className="h-4 w-4"
              >
                <path d="M19 12H5M12 19l-7-7 7-7" />
              </svg>
            </button>
            <button
              type="button"
              onClick={() => goTo(index + 1)}
              disabled={index === total - 1}
              aria-label="Next slide"
              className="flex h-11 min-w-11 items-center justify-center border border-ink text-ink transition-colors duration-(--motion-state-dur) hover:bg-ink hover:text-paper disabled:pointer-events-none disabled:opacity-30 lg:h-10 lg:min-w-10"
            >
              <svg
                aria-hidden="true"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                className="h-4 w-4"
              >
                <path d="M5 12h14M12 5l7 7-7 7" />
              </svg>
            </button>
          </div>
        </div>
      </div>

      {/* One announcement channel for the whole carousel: the plate itself
          never shouts, the slide change does. */}
      <p className="sr-only" aria-live="polite">
        Slide {index + 1} of {total}: {current.name}
      </p>

      {/* Track: one full-bleed plate per product, translated as a whole. */}
      <div className="mt-8 overflow-hidden">
        <div
          className="flex motion-safe:transition-transform motion-safe:duration-(--motion-track-dur) motion-safe:ease-(--motion-ease-track)"
          style={{ transform: `translateX(-${index * 100}%)` }}
        >
          {products.map((product, i) => {
            const inStock = product.stock > 0;
            const active = i === index;
            return (
              <div
                key={product.id}
                role="group"
                aria-roledescription="slide"
                aria-label={`${i + 1} of ${total}`}
                aria-hidden={!active}
                className="w-full shrink-0"
              >
                <div className="grid grid-cols-1 border border-hairline bg-paper lg:grid-cols-12">
                  {/* Flacon plate: the screen's 3/4 frame below lg, stretched
                      by the grid row at lg (code.html:231). Hairline plate,
                      never a shadow — and no priority hint: this sits under
                      the hero, so the hero keeps LCP (§10). */}
                  <div className="relative aspect-[3/4] overflow-hidden border-b border-hairline bg-hairline lg:col-span-5 lg:aspect-auto lg:border-b-0 lg:border-r">
                    <ProductImage
                      src={product.image}
                      alt={product.name}
                      sizes="(min-width: 1024px) 40vw, 100vw"
                    />
                    <span className="absolute left-4 top-4 border border-hairline bg-paper px-2.5 py-1 text-label-sm uppercase text-ink-secondary">
                      N° {pad(i + 1)} · {product.category}
                    </span>
                  </div>

                  {/* Olfactory & acquisition column. The screen shows an
                      olfactory pyramid; there is no notes endpoint (§12-G8
                      class), so the hairline block carries the two facts we
                      DO hold: volume and availability. No invented notes. */}
                  <div className="flex flex-col justify-between gap-8 p-6 sm:p-8 lg:col-span-7 lg:p-16">
                    <div>
                      <div className="flex flex-wrap items-baseline justify-between gap-4">
                        <span className="text-eyebrow uppercase text-bronze">
                          {product.category} · {product.size} ml
                        </span>
                        {/* §2.2.3 / DESIGN.md: the display price is the one
                            serif money moment — tabular so it cannot jitter. */}
                        <span className="font-display text-h2 tabular-nums lining-nums">
                          {formatINR(product.price)}
                        </span>
                      </div>
                      <h3 className="mt-4 font-display text-h1">
                        {product.name}
                      </h3>
                      <p className="mt-4 max-w-(--measure) text-body text-ink-secondary">
                        {product.description}
                      </p>
                      <dl className="mt-6 grid grid-cols-12 gap-2 border-y border-hairline py-4">
                        <dt className="col-span-4 text-label-sm uppercase text-ink-secondary">
                          Volume
                        </dt>
                        <dd className="col-span-8 text-small tabular-nums lining-nums">
                          {product.size} ml
                        </dd>
                        <dt className="col-span-4 text-label-sm uppercase text-ink-secondary">
                          Availability
                        </dt>
                        <dd
                          className={`col-span-8 text-small ${
                            inStock ? "text-success" : "text-ink"
                          }`}
                        >
                          {inStock ? "In stock" : "Sold out"}
                        </dd>
                      </dl>
                    </div>
                    <div>
                      <Link
                        href={`/products/${product.slug}`}
                        tabIndex={active ? undefined : -1}
                        className="inline-flex min-h-12 items-center bg-ink px-8 text-label-sm uppercase text-paper transition-colors duration-(--motion-state-dur) hover:bg-bronze"
                      >
                        View flacon
                        <span className="sr-only">: {product.name}</span>
                      </Link>
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Progress row: four bars, the active one ink, plus the catalogue
          link on the right (screen: "View entire archives"; §5.1 §2 names
          this link "View all", and the site has no archives). Each bar is a
          24px-tall button around a 4px visual, so 2.5.8 is met without
          thickening the screen's hairline bar. */}
      <div className="mt-6 flex flex-wrap items-center justify-between gap-4 border-t border-hairline pt-6">
        <div className="flex items-center gap-2">
          {products.map((product, i) => (
            <button
              key={product.id}
              type="button"
              onClick={() => goTo(i)}
              aria-label={`Go to slide ${i + 1}`}
              aria-current={i === index ? "true" : undefined}
              className="group flex h-6 items-center"
            >
              <span
                className={`block h-1 w-10 transition-colors duration-(--motion-state-dur) ${
                  i === index
                    ? "bg-ink"
                    : "bg-hairline group-hover:bg-hairline-hover"
                }`}
              />
            </button>
          ))}
        </div>
        <Link
          href="/products?ordering=-created_at"
          className="link-underline text-label-sm uppercase text-ink-secondary transition-colors duration-(--motion-state-dur) hover:text-ink"
        >
          View all
        </Link>
      </div>
    </div>
  );
}
