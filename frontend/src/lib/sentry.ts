/**
 * Browser error reporting with a minimal noise denylist.
 *
 * A client error loop (a render that throws on every frame, a chunk that
 * fails to load on every navigation) can emit thousands of events in an
 * hour. Sentry's free tier is a monthly event budget, so an unguarded SDK
 * can spend the whole month in one afternoon and leave the real incidents
 * of the following weeks unsampled. A local per-tab cap was deliberately
 * removed: it went quiet exactly when errors spiked, and the events it
 * dropped were invisible to Sentry. Spend is bounded by Sentry's own
 * controls instead:
 *
 * - Nothing is initialised unless a DSN is configured, so a local run or a
 *   preview build sends nothing.
 * - `NEXT_PUBLIC_SENTRY_SAMPLE_RATE` keeps a fixed fraction of events, and
 *   the Sentry project's rate limit and inbound filters are the ceiling.
 * - `beforeSend` is only a denylist for known noise. It never drops an event
 *   because a local counter says so.
 * - `beforeSend` is wrapped in try/catch: telemetry must never be able to
 *   break the page it is measuring, so a malformed event is dropped, not
 *   thrown.
 *
 * The SDK is `@sentry/browser`, not `@sentry/nextjs`: the repo does not have
 * the Next wrapper, and the wrapper's build plugin would rewrite the bundle
 * for a single error boundary. The lighter browser SDK covers the one call
 * site (`captureException` in `app/error.tsx`) without a build-time hook.
 *
 * Pinned to the v10 line deliberately: v11 removed `sendDefaultPii`, and an
 * explicit "collect no PII" switch that the code can be read for is worth
 * more here than a major-version bump. v10 still supports every option used
 * below.
 */
import * as Sentry from "@sentry/browser";
import type { BrowserOptions, ErrorEvent } from "@sentry/browser";

/** Sentry's default sample rate: every error event is kept. */
export const DEFAULT_SAMPLE_RATE = 1.0;

/**
 * The slice of a Sentry event the guard reads. Kept structural so tests can
 * drive the guard with plain objects instead of fabricating a full `Event`.
 */
export interface SentryEventLike {
  message?: string;
  exception?: {
    values?: Array<{ type?: string; value?: string }>;
  };
}

export interface SentryGuardOptions {
  /** Offline probe; defaults to `navigator.onLine === false`. */
  isOffline?: () => boolean;
}

export interface SentryGuard<T extends SentryEventLike> {
  beforeSend(event: T): T | null;
}

/**
 * Parse the client-side sample rate. Anything that is not a number inside
 * 0.0-1.0 falls back to the default, so a typo in the env file cannot turn
 * sampling into "send nothing" (0) by accident.
 */
export function parseSampleRate(raw: string | undefined): number {
  const trimmed = (raw ?? "").trim();
  if (trimmed === "") return DEFAULT_SAMPLE_RATE;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 1) {
    return DEFAULT_SAMPLE_RATE;
  }
  return parsed;
}

function defaultOffline(): boolean {
  try {
    return typeof navigator !== "undefined" && navigator.onLine === false;
  } catch {
    return false;
  }
}

/**
 * Noise the SDK should never bill for. Each entry is a class of error the
 * runtime recovers from on its own:
 * - ResizeObserver loop warnings are emitted by the browser after layout
 *   settles; nothing is broken and they recur on every resize.
 * - Hydration mismatches are dev-time React/Next diagnostics that a
 *   production build does not even render the same way.
 * - Chunk-load failures are retried by Next on the next navigation, so the
 *   first one is not necessarily a user-visible failure.
 * - `Failed to fetch` while the browser is offline is the expected state of
 *   an offline tab, not a server fault.
 */
export function denylistReason(
  event: SentryEventLike,
  offline: boolean,
): string | null {
  const first = event.exception?.values?.[0];
  const text = [first?.type, first?.value, event.message]
    .filter((part): part is string => typeof part === "string" && part !== "")
    .join(" ");
  if (text === "") return null;
  if (/resizeobserver loop/i.test(text)) return "resize-observer-loop";
  if (
    /hydration failed|error while hydrating|text content does not match server-rendered html|server-rendered html/i.test(
      text,
    )
  ) {
    return "hydration-mismatch";
  }
  if (
    /chunkloaderror|loading (?:css )?chunk \d+ failed|failed to load chunk/i.test(
      text,
    )
  ) {
    return "chunk-load-retry";
  }
  if (
    offline &&
    /failed to fetch|networkerror when attempting to fetch resource|network request failed|load failed/i.test(
      text,
    )
  ) {
    return "offline-network";
  }
  return null;
}

/**
 * Build a guard around the denylist. The default instance below is the one
 * the SDK uses; tests construct isolated instances so one case cannot leak
 * the offline seam into the next.
 */
export function createSentryGuard<T extends SentryEventLike = SentryEventLike>(
  options: SentryGuardOptions = {},
): SentryGuard<T> {
  const isOffline = options.isOffline ?? defaultOffline;

  function beforeSend(event: T): T | null {
    try {
      return denylistReason(event, isOffline()) === null ? event : null;
    } catch {
      // A guard that throws is worse than a dropped event: fail closed.
      return null;
    }
  }

  return { beforeSend };
}

/** The instance `initSentry` hands to the SDK. */
export const sentryGuard: SentryGuard<ErrorEvent> = createSentryGuard<ErrorEvent>();

/** DSN from the environment, or `undefined` when unset/blank. */
export function sentryDsn(): string | undefined {
  const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN?.trim();
  return dsn ? dsn : undefined;
}

/**
 * Sample rate from the environment. The literal `process.env.*` member
 * expression below is what Next.js can statically inline into the browser
 * bundle; a dynamic lookup through a variable would compile to a runtime
 * object access that is always undefined in the client.
 */
export function sentrySampleRate(): number {
  return parseSampleRate(process.env.NEXT_PUBLIC_SENTRY_SAMPLE_RATE);
}

/** The DSN the SDK was last initialised with (null before any init). */
let initializedFor: string | null = null;

/**
 * Initialise the SDK only when a DSN is configured. Idempotent for the same
 * DSN; re-initialises if the configured DSN changes (dev server restarts
 * with a different `.env.local`).
 *
 * `sendDefaultPii: false` and the zeroed trace/replay rates are deliberate:
 * this storefront handles customer data, and the free tier is event-budgeted,
 * so performance and session replay stay off. Sampling is Sentry's own knob,
 * so a deployment can lower its spend without a local counter that would
 * silence errors during an incident.
 */
export function initSentry(): boolean {
  const dsn = sentryDsn();
  if (!dsn) return false;
  if (initializedFor === dsn) return true;
  const options: BrowserOptions = {
    dsn,
    sendDefaultPii: false,
    sampleRate: sentrySampleRate(),
    tracesSampleRate: 0,
    replaysSessionSampleRate: 0,
    replaysOnErrorSampleRate: 0,
    maxBreadcrumbs: 20,
    beforeSend: sentryGuard.beforeSend,
  };
  Sentry.init(options);
  initializedFor = dsn;
  return true;
}

/**
 * Report an error from the client error boundary. Every failure here is
 * swallowed: a telemetry outage (blocked DSN host, ad blocker, bad config)
 * must not turn a handled render error into an unhandled one.
 */
export function captureException(error: unknown): void {
  try {
    if (!initSentry()) return;
    Sentry.captureException(error);
  } catch {
    // See the doc comment: telemetry never breaks the page.
  }
}
