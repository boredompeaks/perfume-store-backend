"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "@/lib/api";
import { createPayment, verifyPayment } from "@/lib/orders-api";
import { loadRazorpay } from "@/lib/razorpay";
import { site } from "@/lib/site";
import { formatINR } from "@/lib/money";
import type { Order, PaymentSession } from "@/lib/types";

/**
 * Payment phases. Structural rule: a 409 from verify means money was
 * captured but the order cannot be fulfilled — the ONLY reachable UI from
 * that state is the contact-support dead end. There is deliberately no
 * retry affordance on it, and no code path re-enters payment/verify from it.
 */
export type PayPhase =
  | { kind: "ready" }
  | { kind: "working"; label: string }
  | { kind: "dismissed" }
  | { kind: "verify-error"; message: string }
  | { kind: "maybe-processed" }
  | { kind: "captured-dead-end" }
  | { kind: "failed"; message: string };

export type PaymentContact = { name?: string; email?: string; phone?: string };

export default function PaymentLauncher({
  order,
  contact,
  buttonLabel,
}: {
  order: Order;
  contact: PaymentContact;
  buttonLabel?: string;
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [phase, setPhase] = useState<PayPhase>({ kind: "ready" });

  const startPayment = () => {
    setPhase({ kind: "working", label: "Contacting payment gateway…" });
    let session: PaymentSession;
    createPayment(order.id)
      .then((s) => {
        session = s;
        return loadRazorpay();
      })
      .then((loaded) => {
        if (!loaded) {
          setPhase({
            kind: "failed",
            message:
              "Could not load the payment gateway. Check your connection and try again.",
          });
          return;
        }
        const rzp = new window.Razorpay!({
          key: session.key_id,
          amount: session.amount,
          currency: session.currency,
          name: site.name,
          description: `Order #${order.id}`,
          order_id: session.razorpay_order_id,
          prefill: {
            name: contact.name,
            email: contact.email,
            contact: contact.phone,
          },
          theme: { color: "#8a5a24" },
          handler: (response) => {
            void confirmPayment(response);
          },
          modal: {
            ondismiss: () => {
              setPhase({ kind: "dismissed" });
            },
          },
        });
        setPhase({
          kind: "working",
          label: "Complete the payment in the secure window.",
        });
        rzp.open();
      })
      .catch((err: unknown) => {
        setPhase({
          kind: "failed",
          message:
            err instanceof ApiError
              ? err.message
              : "Could not start the payment. Try again.",
        });
      });
  };

  const confirmPayment = (response: {
    razorpay_order_id: string;
    razorpay_payment_id: string;
    razorpay_signature: string;
  }) => {
    setPhase({ kind: "working", label: "Confirming your payment…" });
    verifyPayment({
      razorpay_order_id: response.razorpay_order_id,
      razorpay_payment_id: response.razorpay_payment_id,
      razorpay_signature: response.razorpay_signature,
      order_id: order.id,
    })
      .then(() => {
        queryClient.invalidateQueries({ queryKey: ["cart"] });
        queryClient.invalidateQueries({ queryKey: ["orders"] });
        router.push(`/orders/${order.id}?payment=success`);
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError) {
          if (err.status === 409) {
            // Money captured, fulfillment failed, backend cannot reconcile.
            // Dead end: no retry, no further payment/verify calls. Ever.
            setPhase({ kind: "captured-dead-end" });
          } else if (
            err.status === 400 &&
            /already been processed/i.test(err.message)
          ) {
            // A previous verify may have succeeded — never re-open payment.
            setPhase({ kind: "maybe-processed" });
          } else {
            setPhase({ kind: "verify-error", message: err.message });
          }
        } else {
          setPhase({
            kind: "verify-error",
            message: "We lost the connection while confirming. Try again.",
          });
        }
      });
  };

  if (phase.kind === "captured-dead-end") {
    return (
      <div
        data-testid="captured-dead-end"
        className="border border-bronze p-6"
        role="alert"
      >
        <h3 className="font-display text-xl tracking-tight text-ink">
          Payment received — order not completed.
        </h3>
        <p className="mt-3 text-sm text-ink-muted">
          Your payment for order #{order.id} was captured, but the order
          could not be fulfilled (an item became unavailable). Our team
          reconciles this manually — please contact support and quote order
          #{order.id}.
        </p>
        <p className="mt-3 text-sm text-ink">
          Do not retry this payment — it has already been charged.
        </p>
        <Link
          href={`/orders/${order.id}`}
          className="mt-4 inline-block text-sm underline underline-offset-4 transition-colors hover:text-bronze"
        >
          View order status
        </Link>
      </div>
    );
  }

  if (phase.kind === "maybe-processed") {
    return (
      <div className="border border-line p-6" role="alert">
        <h3 className="font-display text-xl tracking-tight">
          Your payment may have gone through.
        </h3>
        <p className="mt-3 text-sm text-ink-muted">
          We couldn&apos;t confirm it just now, and starting another payment
          could double-charge you. Check your orders — if it isn&apos;t
          marked confirmed, contact support.
        </p>
        <Link
          href="/orders"
          className="mt-4 inline-block bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          View orders
        </Link>
      </div>
    );
  }

  if (phase.kind === "working") {
    return (
      <p className="text-sm text-ink-muted" role="status" aria-live="polite">
        {phase.label}
      </p>
    );
  }

  if (phase.kind === "dismissed" || phase.kind === "verify-error") {
    return (
      <div className="border border-line p-6">
        <h3 className="font-display text-xl tracking-tight">
          {phase.kind === "dismissed"
            ? "Payment not completed."
            : "We couldn't confirm your payment."}
        </h3>
        {"message" in phase && phase.message ? (
          <p className="mt-2 text-sm text-bronze">{phase.message}</p>
        ) : (
          <p className="mt-2 text-sm text-ink-muted">
            The order stays pending — nothing was charged yet.
          </p>
        )}
        <div className="mt-4 flex gap-3">
          <button
            type="button"
            data-testid="retry-payment"
            onClick={startPayment}
            className="bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
          >
            Retry payment
          </button>
        </div>
        <p className="mt-3 text-xs text-ink-muted">
          Retrying reuses this order&apos;s existing payment session — you
          won&apos;t be charged twice for a completed payment.
        </p>
      </div>
    );
  }

  if (phase.kind === "failed") {
    return (
      <div className="border border-line p-6" role="alert">
        <p className="text-sm text-bronze">{phase.message}</p>
        <button
          type="button"
          onClick={startPayment}
          className="mt-4 bg-ink px-6 py-3 text-sm text-paper transition-colors hover:bg-bronze"
        >
          Try again
        </button>
      </div>
    );
  }

  // ready
  return (
    <div>
      <button
        type="button"
        data-testid={buttonLabel?.startsWith("Complete") ? "resume-payment" : "pay-button"}
        onClick={startPayment}
        className="bg-ink px-8 py-3 text-sm text-paper transition-colors hover:bg-bronze"
      >
        {buttonLabel ?? `Pay ${formatINR(order.total_amount)}`}
      </button>
      <p className="mt-3 text-xs text-ink-muted">
        A secure Razorpay window will open. Complete the payment there.
      </p>
    </div>
  );
}
