/**
 * Telemetry must never break the page it measures: the guard fails closed, and
 * spend is bounded by DSN, sample rate and Sentry's own limits, not a local
 * counter that would go silent during an incident.
 */
import * as Sentry from "@sentry/browser";
import type { BrowserOptions, ErrorEvent } from "@sentry/browser";

/** Sentry's default sample rate: every error event is kept. */
export const DEFAULT_SAMPLE_RATE = 1.0;

/** Structural slice of an event so tests need not fabricate a full `Event`. */
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
 * Out-of-range or non-numeric values fall back to the default, so a typo
 * cannot silently turn sampling into "send nothing".
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
 * Known noise the runtime recovers from on its own: ResizeObserver loops,
 * dev-only hydration warnings, Next-retried chunk loads and offline fetches.
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

/** Isolated instances let tests inject the offline seam without leaking. */
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
 * The literal `process.env.*` member expression is what Next.js can inline
 * into the browser bundle; a dynamic lookup would always be undefined.
 */
export function sentrySampleRate(): number {
  return parseSampleRate(process.env.NEXT_PUBLIC_SENTRY_SAMPLE_RATE);
}

/** The DSN the SDK was last initialised with (null before any init). */
let initializedFor: string | null = null;

/**
 * No DSN means no init. `sendDefaultPii: false` and zeroed trace/replay rates
 * are deliberate: customer data stays out, and spend is not locally capped.
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
 * Report from the error boundary; every failure here is swallowed so a
 * telemetry outage cannot turn a handled render error into an unhandled one.
 */
export function captureException(error: unknown): void {
  try {
    if (!initSentry()) return;
    Sentry.captureException(error);
  } catch {
    // See the doc comment: telemetry never breaks the page.
  }
}
