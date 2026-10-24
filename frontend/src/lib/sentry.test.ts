import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@sentry/browser", () => ({
  init: vi.fn(),
  captureException: vi.fn(),
}));

import * as Sentry from "@sentry/browser";
import {
  DEFAULT_MAX_EVENTS_PER_HOUR,
  DEDUPE_WINDOW_MS,
  QUOTA_STORAGE_KEY,
  QUOTA_WINDOW_MS,
  captureException,
  createSentryGuard,
  initSentry,
  parseMaxEvents,
  type GuardStorage,
} from "./sentry";

/**
 * P1-3: the FREE-TIER quota guard is the security property of this module.
 * A client error loop must not be able to spend the monthly event budget in
 * an hour, and telemetry must never be able to break the page it measures.
 * These specs drive the guard directly with a fake clock and a fake
 * sessionStorage, so each rule (denylist, dedupe window, cap, persistence,
 * never-throw) is proved in isolation rather than through the real SDK.
 */

type TestEvent = {
  message?: string;
  exception?: { values?: Array<{ type?: string; value?: string }> };
};

function makeGuard(options: { max?: number; offline?: boolean } = {}) {
  const store = new Map<string, string>();
  const storage: GuardStorage = {
    getItem: (key) => (store.has(key) ? store.get(key)! : null),
    setItem: (key, value) => {
      store.set(key, String(value));
    },
  };
  let clock = 1_000_000;
  const now = () => clock;
  const warn = vi.fn();
  const guard = createSentryGuard<TestEvent>({
    maxEventsPerHour: options.max ?? DEFAULT_MAX_EVENTS_PER_HOUR,
    storage: () => storage,
    now,
    warn,
    isOffline: () => options.offline ?? false,
  });
  return {
    guard,
    store,
    storage,
    now,
    warn,
    advance: (ms: number) => {
      clock += ms;
    },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("initialisation gating", () => {
  it("never initialises the SDK without a DSN", () => {
    vi.stubEnv("NEXT_PUBLIC_SENTRY_DSN", "");
    expect(initSentry()).toBe(false);
    expect(Sentry.init).not.toHaveBeenCalled();
  });

  it("initialises once with PII off, zero sampling and the guard attached", () => {
    const dsn = "https://publickey@o1.ingest.sentry.io/1";
    vi.stubEnv("NEXT_PUBLIC_SENTRY_DSN", dsn);
    expect(initSentry()).toBe(true);
    expect(initSentry()).toBe(true);
    expect(Sentry.init).toHaveBeenCalledTimes(1);
    const options = vi.mocked(Sentry.init).mock.calls[0][0]!;
    expect(options).toMatchObject({
      dsn,
      sendDefaultPii: false,
      tracesSampleRate: 0,
      replaysSessionSampleRate: 0,
      replaysOnErrorSampleRate: 0,
      maxBreadcrumbs: 20,
    });
    expect(typeof options.beforeSend).toBe("function");
  });

  it("does not report when no DSN is configured", () => {
    vi.stubEnv("NEXT_PUBLIC_SENTRY_DSN", "");
    expect(() => captureException(new Error("boom"))).not.toThrow();
    expect(Sentry.captureException).not.toHaveBeenCalled();
  });

  it("reports the boundary error through the SDK when a DSN is configured", () => {
    vi.stubEnv("NEXT_PUBLIC_SENTRY_DSN", "https://publickey@o2.ingest.sentry.io/2");
    const error = new Error("boundary");
    captureException(error);
    expect(Sentry.captureException).toHaveBeenCalledWith(error);
  });

  it("swallows an SDK failure instead of breaking the fallback UI", () => {
    vi.stubEnv("NEXT_PUBLIC_SENTRY_DSN", "https://publickey@o3.ingest.sentry.io/3");
    vi.mocked(Sentry.captureException).mockImplementationOnce(() => {
      throw new Error("sdk down");
    });
    expect(() => captureException(new Error("boom"))).not.toThrow();
  });
});

describe("parseMaxEvents", () => {
  it("falls back to 50 for unset, empty, non-numeric and non-positive values", () => {
    expect(DEFAULT_MAX_EVENTS_PER_HOUR).toBe(50);
    for (const raw of [undefined, "", "  ", "0", "-4", "many", "NaN"]) {
      expect(parseMaxEvents(raw)).toBe(50);
    }
  });

  it("honours a positive integer from the environment", () => {
    expect(parseMaxEvents("25")).toBe(25);
    expect(parseMaxEvents(" 7 ")).toBe(7);
  });
});

describe("denylist", () => {
  it("drops the known noise classes", () => {
    const { guard } = makeGuard();
    const noisy: TestEvent[] = [
      { message: "ResizeObserver loop limit exceeded" },
      {
        exception: {
          values: [
            {
              type: "Error",
              value:
                "Hydration failed because the initial UI does not match what was rendered on the server.",
            },
          ],
        },
      },
      {
        exception: {
          values: [{ type: "ChunkLoadError", value: "Loading chunk 12 failed." }],
        },
      },
    ];
    for (const event of noisy) expect(guard.beforeSend(event)).toBeNull();
  });

  it("drops Failed to fetch only while the browser is offline", () => {
    const event: TestEvent = {
      exception: { values: [{ type: "TypeError", value: "Failed to fetch" }] },
    };
    expect(makeGuard({ offline: true }).guard.beforeSend(event)).toBeNull();
    expect(makeGuard({ offline: false }).guard.beforeSend(event)).toBe(event);
  });

  it("keeps a real application error", () => {
    const { guard } = makeGuard();
    const event: TestEvent = {
      exception: {
        values: [
          { type: "TypeError", value: "Cannot read properties of undefined" },
        ],
      },
    };
    expect(guard.beforeSend(event)).toBe(event);
  });
});

describe("dedupe window", () => {
  it("drops an identical (type, message) inside 60s and allows it after", () => {
    const h = makeGuard();
    const event: TestEvent = {
      exception: { values: [{ type: "TypeError", value: "render loop" }] },
    };
    expect(h.guard.beforeSend(event)).toBe(event);
    h.advance(30_000);
    expect(h.guard.beforeSend(event)).toBeNull();
    h.advance(DEDUPE_WINDOW_MS - 30_000 + 1);
    expect(h.guard.beforeSend(event)).toBe(event);
  });

  it("does not collapse two different messages", () => {
    const h = makeGuard();
    expect(h.guard.beforeSend({ message: "first" })).not.toBeNull();
    expect(h.guard.beforeSend({ message: "second" })).not.toBeNull();
  });
});

describe("hourly quota", () => {
  it("drops event N+1 and warns once per window", () => {
    const h = makeGuard({ max: 2 });
    expect(h.guard.beforeSend({ message: "a" })).not.toBeNull();
    expect(h.guard.beforeSend({ message: "b" })).not.toBeNull();
    expect(h.guard.beforeSend({ message: "c" })).toBeNull();
    expect(h.guard.beforeSend({ message: "d" })).toBeNull();
    expect(h.warn).toHaveBeenCalledTimes(1);
  });

  it("persists the counter in sessionStorage and resumes from it", () => {
    const h = makeGuard({ max: 2 });
    h.guard.beforeSend({ message: "a" });
    h.guard.beforeSend({ message: "b" });
    const raw = h.store.get(QUOTA_STORAGE_KEY);
    expect(raw).toBeTruthy();
    expect(JSON.parse(raw!)).toMatchObject({ count: 2 });

    // A second guard has fresh module memory; the tab's storage must still
    // refuse the next event, which is what makes the cap survive a bundle
    // reload inside the same tab.
    const second = createSentryGuard<TestEvent>({
      maxEventsPerHour: 2,
      storage: () => h.storage,
      now: h.now,
      warn: vi.fn(),
      isOffline: () => false,
    });
    expect(second.beforeSend({ message: "c" })).toBeNull();
  });

  it("starts a fresh window after an hour", () => {
    const h = makeGuard({ max: 1 });
    expect(h.guard.beforeSend({ message: "a" })).not.toBeNull();
    expect(h.guard.beforeSend({ message: "b" })).toBeNull();
    h.advance(QUOTA_WINDOW_MS);
    expect(h.guard.beforeSend({ message: "c" })).not.toBeNull();
    expect(h.warn).toHaveBeenCalledTimes(1);
  });
});

describe("never throws", () => {
  it("returns null instead of throwing when reading the event throws", () => {
    const h = makeGuard();
    const hostile = {} as TestEvent;
    Object.defineProperty(hostile, "exception", {
      get() {
        throw new Error("boom");
      },
    });
    expect(() => h.guard.beforeSend(hostile)).not.toThrow();
    expect(h.guard.beforeSend(hostile)).toBeNull();
  });

  it("keeps working when storage is absent or broken", () => {
    const broken = createSentryGuard<TestEvent>({
      storage: () => ({
        getItem() {
          throw new Error("blocked");
        },
        setItem() {
          throw new Error("blocked");
        },
      }),
      now: () => 1_000_000,
    });
    expect(() => broken.beforeSend({ message: "x" })).not.toThrow();
    expect(broken.beforeSend({ message: "y" })).not.toBeNull();

    const absent = createSentryGuard<TestEvent>({
      storage: () => null,
      now: () => 1_000_000,
    });
    expect(absent.beforeSend({ message: "z" })).not.toBeNull();
  });
});

describe("error boundary wiring", () => {
  it("reports the rendered error and leaves the fallback UI intact", () => {
    const source = readFileSync(
      fileURLToPath(new URL("../app/error.tsx", import.meta.url)),
      "utf8",
    );
    expect(source).toMatch(/from "@\/lib\/sentry"/);
    expect(source).toMatch(/captureException\(error\)/);
    expect(source).toContain("Try again");
  });
});
