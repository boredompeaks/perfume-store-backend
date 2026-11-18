"use client";

// Lookup contract (pinned by page.test.ts): explicit submit only, token read once
// and never in state, and a 404 is one uniform miss — no order-existence oracle.
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
  | { kind: "miss"; message: string }
  | { kind: "network" }
  | { kind: "found"; shipments: Shipment[] };

// Presentation of a backend status value only: underscores to spaces, first
// letter up; nothing renamed or invented.
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

  // The select-all plate is the fallback when the clipboard API is unavailable
  // (insecure context or denial).
  const copyWaybill = async () => {
    if (!navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(parcel.tracking_number);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // Select-all still lets the customer copy by hand.
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

      {/* API events are oldest-first; the latest scan is emphasised. */}
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
    // Read once, then cleared: never stored in state or rendered back.
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
      {/* Not <main>: the root layout already renders the single main landmark. */}
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
            {/* Masked credential, uncontrolled so it can be cleared without
                state. */}
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
