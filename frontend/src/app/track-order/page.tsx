"use client";

/**
 * /track-order — public shipment tracking (UI-UX-SPEC §5.6).
 *
 * One centred column on bg-paper: h1, one form (order number + optional
 * guest token), then the parcels the API returns. The lookup is explicit —
 * nothing fetches without a submit — and the credential is never held in
 * React state or rendered back: the token field is uncontrolled and is
 * cleared the instant its value has been read into the request.
 *
 * The miss is ONE box with the backend's message verbatim: a 404 there is
 * byte-identical for "no credential", "wrong token", "no such order",
 * "someone else's order" and "no shipment yet" (_tracking_lookup_miss), and
 * any UI that tried to tell those cases apart would reopen the order-
 * existence oracle the backend closed. A network failure is the only outcome
 * that gets its own copy and a "Try again" affordance.
 *
 * Head tags: a "use client" page cannot export `metadata` (Next.js build
 * error), so <title>/<meta> are rendered directly — the documented React 19
 * alternative. The registry (pages.ts, indexable: false) keeps robots.txt
 * and the sitemap in lockstep with the spec's noindex (§8).
 */
import { useState } from "react";
import { inputClass } from "@/lib/ui";
import {
  trackShipments,
  TrackingMissError,
  type Shipment,
} from "@/lib/tracking-api";

type State =
  | { kind: "idle" }
  | { kind: "pending" }
  /** The uniform miss — server message verbatim, one shape, no branching. */
  | { kind: "miss"; message: string }
  /** The only retryable outcome. */
  | { kind: "network" }
  | { kind: "found"; shipments: Shipment[] };

/**
 * Presentation of a status VALUE, never a new status: the backend's five
 * values (label_created, dispatched, in_transit, delivered, exception)
 * spelled the way its own labels spell them. Underscores to spaces, first
 * letter up; nothing renamed, nothing invented.
 */
function humanizeStatus(value: string): string {
  const flat = value.replace(/_/g, " ").trim();
  return flat.charAt(0).toUpperCase() + flat.slice(1);
}

function formatInstant(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { dateStyle: "medium" });
}

function ParcelBlock({
  parcel,
  index,
  total,
}: {
  parcel: Shipment;
  index: number;
  total: number;
}) {
  const last = parcel.events.length - 1;
  return (
    <article className="border border-hairline p-6">
      {total > 1 && (
        <p className="text-xs font-semibold uppercase tracking-widest text-ink-secondary">
          Parcel {index + 1} of {total}
        </p>
      )}
      <div className="flex items-baseline justify-between gap-4">
        <p className="font-display text-xl text-ink">
          {humanizeStatus(parcel.status)}
        </p>
        <p className="text-xs tabular-nums text-ink-secondary">
          Order {parcel.order_number}
        </p>
      </div>
      <dl className="mt-4 space-y-2 text-sm">
        <div className="flex gap-2">
          <dt className="text-ink-secondary">Carrier</dt>
          <dd className="text-ink">{parcel.carrier}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="text-ink-secondary">Tracking number</dt>
          <dd className="select-all text-sm font-semibold tabular-nums text-ink">
            {parcel.tracking_number}
          </dd>
        </div>
        {parcel.estimated_delivery && (
          <div className="flex gap-2">
            <dt className="text-ink-secondary">Estimated delivery</dt>
            <dd className="tabular-nums text-ink">
              {formatDate(parcel.estimated_delivery)}
            </dd>
          </div>
        )}
      </dl>
      {parcel.events.length > 0 && (
        <ol className="mt-6 space-y-4">
          {parcel.events.map((event, i) => (
            <li key={`${event.status}-${event.occurred_at}-${i}`} className="flex items-start gap-3">
              <span
                aria-hidden="true"
                className={
                  i === last
                    ? "mt-1.5 size-2 shrink-0 rounded-full border border-bronze bg-bronze"
                    : "mt-1.5 size-2 shrink-0 rounded-full border border-hairline"
                }
              />
              <div>
                <p className="text-sm text-ink">{humanizeStatus(event.status)}</p>
                <p className="text-xs tabular-nums text-ink-secondary">
                  {formatInstant(event.occurred_at)}
                </p>
              </div>
            </li>
          ))}
        </ol>
      )}
    </article>
  );
}

export default function TrackOrderPage() {
  const [orderNumber, setOrderNumber] = useState("");
  const [state, setState] = useState<State>({ kind: "idle" });

  const onSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (state.kind === "pending") return;
    const form = e.currentTarget;
    const number = new FormData(form)
      .get("order-number")
      ?.toString()
      .trim() ?? "";
    if (!number) return;
    const tokenField = form.elements.namedItem(
      "track-token",
    ) as HTMLInputElement | null;
    const token = (tokenField?.value ?? "").trim();
    // The credential leaves the DOM here — read once into this call, never
    // stored in state, never rendered back.
    if (tokenField) tokenField.value = "";
    setState({ kind: "pending" });
    try {
      const shipments = await trackShipments(number, token);
      setState({ kind: "found", shipments });
    } catch (err) {
      setState(
        err instanceof TrackingMissError
          ? { kind: "miss", message: err.message }
          : { kind: "network" },
      );
    }
  };

  const retry = () => {
    setState({ kind: "idle" });
    document.getElementById("track-order-number")?.focus();
  };

  return (
    <>
      <title>Track your order</title>
      <meta name="robots" content="noindex, follow" />
      <meta
        name="description"
        content="Look up where your parcel is using your order number."
      />
      <main className="mx-auto w-full max-w-[calc(var(--measure)_+_4rem)] px-4 py-16 sm:px-6">
        <h1 className="font-display text-4xl tracking-tight">Track your order</h1>
        <p className="mt-4 max-w-[var(--measure)] text-sm text-ink-secondary">
          See where your parcel is. You need your order number and the order
          token from your confirmation. Signed in? The token is optional.
        </p>
        <form onSubmit={onSubmit} className="mt-8 space-y-4" noValidate>
          <div>
            <label htmlFor="track-order-number" className="mb-1 block text-sm">
              Order number
            </label>
            <input
              id="track-order-number"
              name="order-number"
              type="text"
              autoComplete="off"
              spellCheck={false}
              value={orderNumber}
              onChange={(e) => setOrderNumber(e.target.value)}
              className={`${inputClass} w-full`}
              required
            />
          </div>
          <div>
            <label htmlFor="track-token" className="mb-1 block text-sm">
              Order token (optional)
            </label>
            <input
              id="track-token"
              name="track-token"
              type="password"
              autoComplete="off"
              className={`${inputClass} w-full`}
            />
            <p className="mt-1 text-xs text-ink-secondary">
              Shown on your order confirmation. If you are signed in, you can
              leave this blank.
            </p>
          </div>
          <button
            type="submit"
            disabled={state.kind === "pending" || !orderNumber.trim()}
            className="w-full bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze disabled:opacity-60"
          >
            {state.kind === "pending" ? "Tracking…" : "Track order"}
          </button>
        </form>
        <div aria-live="polite">
          {state.kind === "pending" && (
            <p role="status" className="mt-8 text-sm text-ink-secondary">
              Looking up your parcel…
            </p>
          )}
          {state.kind === "miss" && (
            <div
              role="alert"
              className="mt-8 border border-hairline p-6 text-sm text-ink"
            >
              {state.message}
            </div>
          )}
          {state.kind === "network" && (
            <div role="alert" className="mt-8 border border-hairline p-6">
              <p className="text-sm text-ink">
                We could not reach the store. Check your connection and try
                again.
              </p>
              <button
                type="button"
                onClick={retry}
                className="mt-4 border border-ink px-4 py-2 text-sm transition-colors hover:bg-ink hover:text-paper"
              >
                Try again
              </button>
            </div>
          )}
          {state.kind === "found" && (
            <div className="mt-8 space-y-6 transition-[opacity,transform] duration-(--motion-enter-dur) ease-(--motion-ease) starting:translate-y-2 starting:opacity-0">
              {state.shipments.map((parcel, i) => (
                <ParcelBlock
                  key={`${parcel.order_number}-${parcel.tracking_number}-${i}`}
                  parcel={parcel}
                  index={i}
                  total={state.shipments.length}
                />
              ))}
            </div>
          )}
        </div>
      </main>
    </>
  );
}
