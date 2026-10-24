/**
 * Browser error reporting with a hard FREE-TIER quota guard.
 *
 * A client error loop (a render that throws on every frame, a chunk that
 * fails to load on every navigation) can emit thousands of events in an
 * hour. Sentry's free tier is a monthly event budget, so an unguarded SDK
 * can spend the whole month in one afternoon and leave the real incidents
 * of the following weeks unsampled. Everything below exists to make that
 * impossible rather than unlikely:
 *
 * - Nothing is initialised unless a DSN is configured, so a local run or a
 *   preview build sends nothing.
 * - `beforeSend` is the single chokepoint. It drops known noise, collapses
 *   identical events inside a 60s window, and refuses everything past a
 *   per-tab hourly cap that survives in sessionStorage.
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

/** Free-tier safety default: 50 events per tab per rolling hour. */
export const DEFAULT_MAX_EVENTS_PER_HOUR = 50;
/** Identical (type, message) pairs inside this window are sent once. */
export const DEDUPE_WINDOW_MS = 60_000;
/** The quota window; anchored to the first event of the window, not to the clock hour. */
export const QUOTA_WINDOW_MS = 3_600_000;
/** sessionStorage key holding `{ windowStart, count }` for this tab. */
export const QUOTA_STORAGE_KEY = "aurel.sentry-quota";

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

export interface GuardStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export interface SentryGuardOptions {
  /** Events allowed per window. Defaults to the env value, then 50. */
  maxEventsPerHour?: number;
  /** Clock seam; defaults to `Date.now`. */
  now?: () => number;
  /** Storage seam; defaults to `window.sessionStorage` when it exists. */
  storage?: () => GuardStorage | null;
  /** Warning sink; defaults to `console.warn`. */
  warn?: (message: string) => void;
  /** Offline probe; defaults to `navigator.onLine === false`. */
  isOffline?: () => boolean;
}

export interface SentryGuard<T extends SentryEventLike> {
  beforeSend(event: T): T | null;
}

interface QuotaRecord {
  windowStart: number;
  count: number;
}

/**
 * Parse the hourly cap. Anything that is not a positive integer falls back
 * to the default, so a typo in the env file cannot disable the guard by
 * producing `NaN` (every comparison with `NaN` is false).
 */
export function parseMaxEvents(raw: string | undefined): number {
  const parsed = Number.parseInt((raw ?? "").trim(), 10);
  if (!Number.isFinite(parsed) || parsed < 1) return DEFAULT_MAX_EVENTS_PER_HOUR;
  return parsed;
}

function defaultStorage(): GuardStorage | null {
  try {
    if (typeof window === "undefined") return null;
    return window.sessionStorage ?? null;
  } catch {
    // Private mode / blocked storage: the guard falls back to module memory.
    return null;
  }
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
 * Dedupe identity: the exception type plus the message. An event carrying
 * neither has no identity, so it is never deduped (and never collapsed into
 * an unrelated blank-key event).
 */
function identityKey(event: SentryEventLike): string | null {
  const first = event.exception?.values?.[0];
  const type = first?.type ?? "";
  const message = event.message ?? first?.value ?? "";
  if (type === "" && message === "") return null;
  return `${type}\u0000${message}`;
}

function isQuotaRecord(value: unknown): value is QuotaRecord {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Partial<QuotaRecord>;
  return (
    typeof record.windowStart === "number" &&
    Number.isFinite(record.windowStart) &&
    typeof record.count === "number" &&
    Number.isFinite(record.count)
  );
}

/**
 * Build a guard with its own counter, dedupe map and warning state. The
 * default instance below is the one the SDK uses; tests construct isolated
 * instances so one case cannot leak state into the next.
 */
export function createSentryGuard<T extends SentryEventLike = SentryEventLike>(
  options: SentryGuardOptions = {},
): SentryGuard<T> {
  const max = options.maxEventsPerHour ?? DEFAULT_MAX_EVENTS_PER_HOUR;
  const now = options.now ?? (() => Date.now());
  const storage = options.storage ?? defaultStorage;
  const warn =
    options.warn ??
    ((message: string) => {
      console.warn(message);
    });
  const isOffline = options.isOffline ?? defaultOffline;

  /** Module-side copy of the persisted counter, used when storage is absent. */
  let counter: QuotaRecord | null = null;
  /** windowStart whose cap warning has already been printed. */
  let warnedWindow: number | null = null;
  /** Last send time per identity, within `DEDUPE_WINDOW_MS`. */
  const lastSent = new Map<string, number>();

  function readStored(): QuotaRecord | null {
    try {
      const raw = storage()?.getItem(QUOTA_STORAGE_KEY);
      if (!raw) return null;
      const parsed: unknown = JSON.parse(raw);
      if (!isQuotaRecord(parsed)) return null;
      return {
        windowStart: parsed.windowStart,
        count: Math.max(0, Math.floor(parsed.count)),
      };
    } catch {
      return null;
    }
  }

  function persist(record: QuotaRecord): void {
    counter = record;
    try {
      storage()?.setItem(QUOTA_STORAGE_KEY, JSON.stringify(record));
    } catch {
      // Write failure only costs cross-call persistence; module memory still
      // enforces the cap for the life of this bundle.
    }
  }

  /**
   * The live record for `ts`. sessionStorage is the shared authority between
   * the tab's calls; the module copy is merged in because a failed write
   * must not let the counter go backwards. A record older than the window
   * (or a clock that moved backwards) starts a fresh window.
   */
  function currentRecord(ts: number): QuotaRecord {
    const candidates: QuotaRecord[] = [];
    const stored = readStored();
    if (stored) candidates.push(stored);
    if (counter) candidates.push(counter);
    const live = candidates.filter(
      (record) =>
        ts >= record.windowStart && ts - record.windowStart < QUOTA_WINDOW_MS,
    );
    if (live.length === 0) return { windowStart: ts, count: 0 };
    const windowStart = Math.min(...live.map((record) => record.windowStart));
    const count = Math.max(
      ...live
        .filter((record) => record.windowStart === windowStart)
        .map((record) => record.count),
    );
    return { windowStart, count };
  }

  function prune(ts: number): void {
    for (const [key, sentAt] of lastSent) {
      if (ts - sentAt >= DEDUPE_WINDOW_MS || ts < sentAt) lastSent.delete(key);
    }
  }

  function beforeSend(event: T): T | null {
    try {
      if (denylistReason(event, isOffline()) !== null) return null;
      const ts = now();
      const key = identityKey(event);
      if (key !== null) {
        const sentAt = lastSent.get(key);
        if (
          sentAt !== undefined &&
          ts >= sentAt &&
          ts - sentAt < DEDUPE_WINDOW_MS
        ) {
          return null;
        }
      }
      const record = currentRecord(ts);
      if (record.count >= max) {
        // One warning per window, not one per dropped event: a loop would
        // otherwise fill the console it is being reported through.
        if (warnedWindow !== record.windowStart) {
          warnedWindow = record.windowStart;
          warn(
            `[sentry] hourly event quota reached (${max}); dropping events until the window resets`,
          );
        }
        return null;
      }
      persist({ windowStart: record.windowStart, count: record.count + 1 });
      if (key !== null) lastSent.set(key, ts);
      prune(ts);
      return event;
    } catch {
      // A guard that throws is worse than a dropped event: fail closed.
      return null;
    }
  }

  return { beforeSend };
}

/**
 * The instance `initSentry` hands to the SDK.
 *
 * The literal `process.env.*` member expression below is what Next.js can
 * statically inline into the browser bundle; a dynamic lookup through a
 * variable would compile to a runtime object access that is always undefined
 * in the client.
 */
export const sentryGuard: SentryGuard<ErrorEvent> = createSentryGuard<ErrorEvent>({
  maxEventsPerHour: parseMaxEvents(
    process.env.NEXT_PUBLIC_SENTRY_MAX_EVENTS_PER_HOUR,
  ),
});

/** DSN from the environment, or `undefined` when unset/blank. */
export function sentryDsn(): string | undefined {
  const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN?.trim();
  return dsn ? dsn : undefined;
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
 * so performance and session replay stay off.
 */
export function initSentry(): boolean {
  const dsn = sentryDsn();
  if (!dsn) return false;
  if (initializedFor === dsn) return true;
  const options: BrowserOptions = {
    dsn,
    sendDefaultPii: false,
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
