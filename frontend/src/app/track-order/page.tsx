"use client";

/**
 * /track-order — public shipment tracking (UI-UX-SPEC §5.6), dressed to
 * the D1 screen (maison_ther_suivi_public_de_commande): a bronze
 * registry eyebrow, serif display h1, one ledger-column form, then the
 * lookup result below a hairline seam as a registry record.
 *
 * The lookup contract is unchanged and is pinned by page.test.ts: the
 * request fires only on explicit submit (`GET /api/shipping/track/
 * {order_number}/` with `X-Guest-Order-Token` when a token was given),
 * the credential never enters React state and the field is cleared the
 * instant its value has been read, and a miss renders the backend's
 * message VERBATIM in one panel. A 404 there is byte-identical for "no
 * credential", "wrong token", "no such order", "someone else's order"
 * and "no shipment yet" (_tracking_lookup_miss); any UI that told those
 * cases apart would reopen the order-existence oracle the backend
 * closed. A network failure is the only outcome with its own copy and
 * a retry affordance.
 *
 * The result panel speaks the same vocabulary as the order detail
 * page's ShipmentTracking (W12-T9): backend statuses humanised, events
 * oldest-first in the API's append order with the latest emphasised,
 * a waybill plate (select-all + copy), and the checkpoint rail. Where
 * this page departs from the screen: the token field stays masked (the
 * screen shows it in clear; it is a credential), the screen's
 * "simulate not found" button is mockup chrome and does not ship, the
 * staggered per-node timeline cascade is replaced by the one
 * token-driven settle the result container already had, and the
 * archival certification stamp is omitted (the claim would be
 * fictional).
 *
 * Head tags live on the server segment layout (./layout.tsx): a
 * "use client" page cannot export `metadata`, and the manual tags a
 * client page renders are shadowed by the root layout's defaults.
 * Title "Track your order", canonical /track-order, `noindex, follow`
 * (§8.1; robots.txt + sitemap exclusion ride the same pages.ts row).
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
  const [copied, setCopied] = useState(false);

  // Convenience over the plate's select-all; the selection fallback stands
  // when the clipboard API is unavailable (insecure context, denial).
  const copyWaybill = async () => {
    if (!navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(parcel.tracking_number);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // select-all on the number still lets the customer copy by hand.
    }
  };

  return (
    <article className="space-y-6">
      <div>
        <p className="text-eyebrow uppercase text-bronze">
          {total > 1
            ? `Order ${parcel.order_number} · Parcel ${index + 1} of ${total}`
            : `Order ${parcel.order_number}`}
        </p>
        <h2 className="mt-2 font-display text-h2 tracking-tight">
          {total > 1 ? `Parcel ${index + 1} of ${total}: ` : ""}
          {humanizeStatus(parcel.status)}
        </h2>
        <p className="mt-2 text-small text-ink-secondary">
          Carrier: {parcel.carrier}
        </p>
      </div>

      {/* Waybill plate: hairline frame, select-all tabular digits, copy. */}
      <div className="flex flex-wrap items-center justify-between gap-3 border border-hairline px-4 py-3">
        <p className="text-small">
          <span className="uppercase text-ink-secondary">Waybill</span>{" "}
          <span className="select-all text-label tabular-nums lining-nums text-ink">
            {parcel.tracking_number}
          </span>
        </p>
        <button
          type="button"
          onClick={copyWaybill}
          className="border border-field-border px-3 py-1 text-label-sm uppercase text-ink-secondary transition-colors hover:border-ink hover:text-ink"
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>

      {/* The one bronze accent on this page: the arrival horizon. */}
      {parcel.estimated_delivery && (
        <div className="border-l-2 border-bronze py-1 pl-4">
          <p className="text-eyebrow uppercase text-ink-secondary">
            Estimated delivery
          </p>
          <p className="mt-1 text-body tabular-nums lining-nums text-ink">
            {formatDate(parcel.estimated_delivery)}
          </p>
        </div>
      )}

      {/* Checkpoint rail, the order-detail vocabulary (W12-T9): the API's
          append order is oldest-first, and the latest scan is emphasised. */}
      {parcel.events.length > 0 && (
        <div>
          <div className="border-b border-hairline pb-2">
            <h3 className="text-eyebrow uppercase text-ink">Checkpoints</h3>
          </div>
          <ol className="mt-4 space-y-6 border-l border-hairline pl-4">
            {parcel.events.map((event, i) => (
              <li
                key={`${event.status}-${event.occurred_at}-${i}`}
                className="flex flex-col gap-0.5 sm:flex-row sm:items-baseline sm:gap-4"
              >
                <span className="w-36 shrink-0 text-small tabular-nums lining-nums text-ink-secondary">
                  {formatInstant(event.occurred_at)}
                </span>
                <span
                  className={
                    i === last
                      ? "text-body font-medium text-ink"
                      : "text-body text-ink-secondary"
                  }
                >
                  {humanizeStatus(event.status)}
                </span>
              </li>
            ))}
          </ol>
        </div>
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
      {/* Plain container, not <main>: the layout already renders the page's
          single <main id="main"> (layout.tsx) and every other page nests
          inside it. max-w-2xl is the screen's ledger column (code.html
          line 24), wider than the old measure-bound form. */}
      <div className="mx-auto w-full max-w-2xl px-4 py-16 sm:px-6">
        <p className="text-eyebrow uppercase text-bronze">
          Public shipping registry
        </p>
        <h1 className="mt-3 font-display text-display tracking-tight">
          Track your order
        </h1>
        <p className="mt-4 text-body text-ink-secondary">
          See where your parcel is. You need your order number, plus the
          order token from your confirmation if you checked out without an
          account. Signed in? The token is optional.
        </p>
        <form onSubmit={onSubmit} className="mt-8 space-y-6" noValidate>
          <div>
            <label
              htmlFor="track-order-number"
              className="mb-2 block text-label-sm uppercase text-ink-secondary"
            >
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
            <label
              htmlFor="track-token"
              className="mb-2 block text-label-sm uppercase text-ink-secondary"
            >
              Order token (optional)
            </label>
            {/* Masked, unlike the screen's clear fixture: the token is a
                credential (see docblock). Uncontrolled by design. */}
            <input
              id="track-token"
              name="track-token"
              type="password"
              autoComplete="off"
              className={`${inputClass} w-full`}
            />
            <p className="mt-2 text-small text-ink-secondary">
              Shown on your order confirmation. If you are signed in, you can
              leave this blank.
            </p>
          </div>
          <div className="pt-1">
            <button
              type="submit"
              disabled={state.kind === "pending" || !orderNumber.trim()}
              className="w-full bg-ink px-8 py-4 text-label uppercase text-paper transition-colors hover:bg-bronze disabled:opacity-60"
            >
              {state.kind === "pending" ? "Looking up…" : "Track order"}
            </button>
          </div>
        </form>
        <div aria-live="polite">
          {state.kind === "pending" && (
            <p role="status" className="mt-10 text-body text-ink-secondary">
              Looking up your parcel…
            </p>
          )}
          {state.kind === "miss" && (
            <div
              role="alert"
              className="mt-10 border border-hairline p-6 text-body text-ink"
            >
              {state.message}
            </div>
          )}
          {state.kind === "network" && (
            <div role="alert" className="mt-10 border border-hairline p-6">
              <p className="text-body text-ink">
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
            <div className="mt-10 space-y-10 border-t border-hairline pt-10 transition-[opacity,transform] duration-(--motion-enter-dur) ease-(--motion-ease) starting:translate-y-2 starting:opacity-0">
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
      </div>
    </>
  );
}
