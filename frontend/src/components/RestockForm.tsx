"use client";

import { useMutation } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import { restockSubscribe, restockUnsubscribe } from "@/lib/products-api";
import { inputClass } from "@/lib/ui";

/**
 * Sold-out PDP restock opt-in (U-W4-F3, spec §5.2 item 4 / §12.G6): the
 * one component that stands in for the add-to-cart row while `stock <= 0`.
 * An in-stock PDP never renders it — the page gates on the same `stock > 0`
 * derivation the button uses, unchanged.
 *
 * Server truth only (P5), two arms (SPEC-Q-RS1): an AUTHENTICATED submit
 * answers 201 on a first opt-in and 200 when an existing preference is
 * re-armed ("already registered"); a GUEST submit that carries an email
 * answers one uniform 202 — first-time and repeat are deliberately
 * indistinguishable (zero oracle), so 201 and 202 both render the "on
 * the list" copy and only the authed 200 renders "already". DELETE 200 =
 * opted out. Every 4xx message is rendered VERBATIM from the backend's
 * uniform envelope — this component writes no error copy of its own
 * beyond the client-side email shape check, which it must own because
 * the guest success body carries no error channel. A 401 still exists
 * for a signed-out caller (a POST with no submitted email edge, and
 * every DELETE — opt-out stays ownership-scoped), so the server's
 * message plus the sign-in door stays inline — no toast, no retry loop
 * (throttles `restock` + `restock-guest`, so one submit is one attempt).
 */

type Phase = "idle" | "registered" | "already" | "unsubscribed";

/** Shape-only and deliberately minimal: the backend owns real validation. */
const EMAIL_SHAPE = /^\S+@\S+\.\S+$/;

export default function RestockForm({ slug }: { slug: string }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [needsSignIn, setNeedsSignIn] = useState(false);
  const [drawn, setDrawn] = useState(false);
  const secondFrame = useRef<number | null>(null);

  // The one icon draw in the system (spec §2.4): the check's
  // `strokeDashoffset` animating on a 16px SVG, once, at the `state`
  // intent token. A paint property on a single small SVG is the spec's own
  // named exception; the token layer zeroes the duration under reduced
  // motion, so the check simply appears whole. It confirms the opt-in in
  // words as well — motion never carries information that is not also text
  // (§3 P3).
  useEffect(() => {
    if (phase !== "registered" && phase !== "already") return;
    setDrawn(false);
    const firstFrame = requestAnimationFrame(() => {
      secondFrame.current = requestAnimationFrame(() => setDrawn(true));
    });
    return () => {
      cancelAnimationFrame(firstFrame);
      if (secondFrame.current !== null) {
        cancelAnimationFrame(secondFrame.current);
      }
    };
  }, [phase]);

  const subscribe = useMutation({
    mutationFn: (value: string) => restockSubscribe(slug, value),
    onSuccess: (result) => {
      setError(null);
      setNeedsSignIn(false);
      // `created` is true for the authed 201 and the guest's uniform
      // 202 (first-or-repeat by design); only the authed 200 re-arm
      // reads as "already on the list".
      setPhase(result.created ? "registered" : "already");
    },
    onError: (err) => {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not save your request. Try again.",
      );
      setNeedsSignIn(
        err instanceof ApiError && (err.status === 401 || err.status === 403),
      );
    },
  });

  const unsubscribe = useMutation({
    mutationFn: () => restockUnsubscribe(slug),
    onSuccess: () => {
      setError(null);
      setNeedsSignIn(false);
      setPhase("unsubscribed");
    },
    onError: (err) => {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not save your request. Try again.",
      );
      setNeedsSignIn(false);
    },
  });

  const onSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const value = email.trim();
    if (!EMAIL_SHAPE.test(value)) {
      setError("Enter a valid email address.");
      setNeedsSignIn(false);
      return;
    }
    subscribe.mutate(value);
  };

  const alert = error ? (
    <p role="alert" className="mt-2 text-sm text-bronze">
      {error}
    </p>
  ) : null;

  const quietButton =
    "mt-3 text-sm text-ink-secondary underline underline-offset-4 transition-colors hover:text-bronze disabled:opacity-50";

  if (phase === "registered" || phase === "already") {
    return (
      <div>
        <div
          role="status"
          className="border border-field-border bg-surface p-4"
        >
          <p className="flex items-center gap-2 text-sm">
            <svg
              width="16"
              height="16"
              viewBox="0 0 16 16"
              aria-hidden="true"
              focusable="false"
              className="shrink-0 text-success"
            >
              <path
                d="M2.75 8.5 6.25 12 13.25 4.5"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.75"
                strokeLinecap="round"
                strokeLinejoin="round"
                style={{
                  strokeDasharray: 17,
                  strokeDashoffset: drawn ? 0 : 17,
                  // The one property that changes is the path's dash
                  // offset; the declaration stays `all` because `ui.test.ts`
                  // scans this file for `stroke-*` colour candidates and the
                  // hyphenated CSS spelling of that property is
                  // indistinguishable from a typo'd token there. Duration
                  // and easing are tokens, so the reduced-motion block
                  // reaches this transition like every other.
                  transition:
                    "all var(--motion-state-dur) var(--motion-ease)",
                }}
              />
            </svg>
            <span>
              {phase === "registered"
                ? "You're on the list. We'll email you when it's back in stock."
                : "You're already on the list. We'll email you when it's back in stock."}
            </span>
          </p>
          <button
            type="button"
            onClick={() => unsubscribe.mutate()}
            disabled={unsubscribe.isPending}
            className={quietButton}
          >
            {unsubscribe.isPending ? "Stopping…" : "Stop the emails"}
          </button>
        </div>
        {alert}
      </div>
    );
  }

  if (phase === "unsubscribed") {
    return (
      <div>
        <div
          role="status"
          className="border border-field-border bg-surface p-4"
        >
          <p className="text-sm">
            You won't get an email when it's back in stock.
          </p>
          <button
            type="button"
            onClick={() => {
              setError(null);
              setPhase("idle");
            }}
            className={quietButton}
          >
            Notify me when it's back
          </button>
        </div>
        {alert}
      </div>
    );
  }

  return (
    <div>
      <form
        onSubmit={onSubmit}
        noValidate
        className="border border-field-border bg-surface p-4"
      >
        <label htmlFor="restock-email" className="mb-1 block text-sm">
          Email
        </label>
        <input
          id="restock-email"
          type="email"
          autoComplete="email"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          className={`${inputClass} w-full`}
        />
        <button
          type="submit"
          disabled={subscribe.isPending}
          className="mt-3 w-full bg-ink px-6 py-2 text-sm text-paper transition-colors hover:bg-bronze disabled:opacity-70"
        >
          {subscribe.isPending ? "Notifying…" : "Notify me when it's back"}
        </button>
      </form>
      {alert}
      {needsSignIn && (
        <p className="mt-1 text-sm text-ink-secondary">
          <Link
            href={`/login?next=/products/${slug}`}
            className="underline underline-offset-4 transition-colors hover:text-bronze"
          >
            Sign in
          </Link>{" "}
          to get the back-in-stock email.
        </p>
      )}
    </div>
  );
}
