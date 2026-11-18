import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@sentry/browser", () => ({
  init: vi.fn(),
  captureException: vi.fn(),
}));

import * as Sentry from "@sentry/browser";
import {
  DEFAULT_SAMPLE_RATE,
  captureException,
  createSentryGuard,
  initSentry,
  parseSampleRate,
  type SentryEventLike,
} from "./sentry";

/**
 * No local per-tab cap (it would silence the spike it should reveal); the
 * denylist and fail-closed guard are proved here.
 */

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

  it("initialises once with PII off, zero tracing and a full sample rate", () => {
    const dsn = "https://publickey@o1.ingest.sentry.io/1";
    vi.stubEnv("NEXT_PUBLIC_SENTRY_DSN", dsn);
    vi.stubEnv("NEXT_PUBLIC_SENTRY_SAMPLE_RATE", "");
    expect(initSentry()).toBe(true);
    expect(initSentry()).toBe(true);
    expect(Sentry.init).toHaveBeenCalledTimes(1);
    const options = vi.mocked(Sentry.init).mock.calls[0][0]!;
    expect(options).toMatchObject({
      dsn,
      sendDefaultPii: false,
      sampleRate: DEFAULT_SAMPLE_RATE,
      tracesSampleRate: 0,
      replaysSessionSampleRate: 0,
      replaysOnErrorSampleRate: 0,
      maxBreadcrumbs: 20,
    });
    expect(typeof options.beforeSend).toBe("function");
  });

  it("honours the configured sample rate", () => {
    vi.stubEnv(
      "NEXT_PUBLIC_SENTRY_DSN",
      "https://publickey@o2.ingest.sentry.io/2",
    );
    vi.stubEnv("NEXT_PUBLIC_SENTRY_SAMPLE_RATE", "0.25");
    expect(initSentry()).toBe(true);
    expect(vi.mocked(Sentry.init).mock.calls[0][0]!.sampleRate).toBe(0.25);
  });

  it("does not report when no DSN is configured", () => {
    vi.stubEnv("NEXT_PUBLIC_SENTRY_DSN", "");
    expect(() => captureException(new Error("boom"))).not.toThrow();
    expect(Sentry.captureException).not.toHaveBeenCalled();
  });

  it("reports the boundary error through the SDK when a DSN is configured", () => {
    vi.stubEnv(
      "NEXT_PUBLIC_SENTRY_DSN",
      "https://publickey@o3.ingest.sentry.io/3",
    );
    const error = new Error("boundary");
    captureException(error);
    expect(Sentry.captureException).toHaveBeenCalledWith(error);
  });

  it("swallows an SDK failure instead of breaking the fallback UI", () => {
    vi.stubEnv(
      "NEXT_PUBLIC_SENTRY_DSN",
      "https://publickey@o4.ingest.sentry.io/4",
    );
    vi.mocked(Sentry.captureException).mockImplementationOnce(() => {
      throw new Error("sdk down");
    });
    expect(() => captureException(new Error("boom"))).not.toThrow();
  });
});

describe("sample rate", () => {
  it("defaults to 1.0 for unset, empty, non-numeric and out-of-range values", () => {
    expect(DEFAULT_SAMPLE_RATE).toBe(1);
    for (const raw of [undefined, "", "  ", "many", "NaN", "-0.1", "1.5"]) {
      expect(parseSampleRate(raw)).toBe(1);
    }
  });

  it("accepts a number in 0.0-1.0, including both bounds", () => {
    expect(parseSampleRate("0")).toBe(0);
    expect(parseSampleRate("0.25")).toBe(0.25);
    expect(parseSampleRate(" 1 ")).toBe(1);
  });
});

describe("denylist", () => {
  it("drops the known noise classes", () => {
    const guard = createSentryGuard();
    const noisy: SentryEventLike[] = [
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
    const event: SentryEventLike = {
      exception: { values: [{ type: "TypeError", value: "Failed to fetch" }] },
    };
    expect(
      createSentryGuard({ isOffline: () => true }).beforeSend(event),
    ).toBeNull();
    expect(
      createSentryGuard({ isOffline: () => false }).beforeSend(event),
    ).toBe(event);
  });

  it("keeps a real application error", () => {
    const event: SentryEventLike = {
      exception: {
        values: [
          { type: "TypeError", value: "Cannot read properties of undefined" },
        ],
      },
    };
    expect(createSentryGuard().beforeSend(event)).toBe(event);
  });

  it("keeps events with no identity", () => {
    const event: SentryEventLike = {};
    expect(createSentryGuard().beforeSend(event)).toBe(event);
  });
});

describe("no local ceiling", () => {
  it("does not collapse identical events", () => {
    const guard = createSentryGuard({ isOffline: () => false });
    const event: SentryEventLike = {
      exception: { values: [{ type: "TypeError", value: "render loop" }] },
    };
    for (let index = 0; index < 60; index += 1) {
      expect(guard.beforeSend(event)).toBe(event);
    }
  });
});

describe("never throws", () => {
  it("returns null instead of throwing when reading the event throws", () => {
    const guard = createSentryGuard();
    const hostile = {} as SentryEventLike;
    Object.defineProperty(hostile, "exception", {
      get() {
        throw new Error("boom");
      },
    });
    expect(() => guard.beforeSend(hostile)).not.toThrow();
    expect(guard.beforeSend(hostile)).toBeNull();
  });

  it("survives a browser whose navigator probe throws", () => {
    vi.stubGlobal("navigator", {
      get onLine() {
        throw new Error("blocked");
      },
    });
    const guard = createSentryGuard();
    expect(guard.beforeSend({ message: "still reported" })).not.toBeNull();
  });

  it("reads the offline state from navigator by default", () => {
    vi.stubGlobal("navigator", { onLine: false });
    const guard = createSentryGuard();
    expect(
      guard.beforeSend({
        exception: { values: [{ type: "TypeError", value: "Failed to fetch" }] },
      }),
    ).toBeNull();
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
