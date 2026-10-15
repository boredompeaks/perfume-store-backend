import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { API_BASE } from "./config";

/**
 * Policy binding contract (spec A-12.D.2): `getReturnWindow` renders a number
 * ONLY from a positive `return_window_days` on `GET /api/settings/`. Absent
 * field, malformed values (strings, the closed-window 0, negatives, junk
 * bodies), non-2xx replies and a dead backend must all resolve to `{}` —
 * there is no default on this path, because a fabricated window is the exact
 * contradiction the rule exists to prevent.
 */
function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

type FetchReply = (input: unknown, init?: RequestInit) => Promise<Response>;

function stubFetch(reply: FetchReply) {
  vi.stubGlobal("fetch", vi.fn(reply));
}

beforeEach(() => {
  vi.resetModules();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function getReturnWindow() {
  return (await import("./policies")).getReturnWindow;
}

describe("getReturnWindow", () => {
  it("passes a positive number through, from /api/settings/ with the 60s cache", async () => {
    let seenUrl = "";
    let seenInit: RequestInit | undefined;
    stubFetch(async (input, init) => {
      seenUrl = String(input);
      seenInit = init;
      return jsonResponse({ support_email: "", return_window_days: 45 });
    });

    await expect((await getReturnWindow())()).resolves.toEqual({ days: 45 });
    expect(seenUrl).toBe(`${API_BASE}/api/settings/`);
    expect(seenInit?.next).toEqual({ revalidate: 60 });
  });

  it("absent field: contact channels only => no days", async () => {
    stubFetch(async () =>
      jsonResponse({ support_email: "hi@example.com", support_phone: "" }),
    );

    await expect((await getReturnWindow())()).resolves.toEqual({});
  });

  it("malformed values => no days (string, zero/closed, negative, null, non-integer)", async () => {
    const bodies = [
      { return_window_days: "7" },
      { return_window_days: 0 },
      { return_window_days: -1 },
      { return_window_days: null },
      { return_window_days: true },
      { return_window_days: 45.5 },
      {},
    ];
    for (const body of bodies) {
      stubFetch(async () => jsonResponse(body));
      await expect((await getReturnWindow())()).resolves.toEqual({});
    }
  });

  it("non-object body => no days", async () => {
    stubFetch(async () => jsonResponse("return_window_days"));

    await expect((await getReturnWindow())()).resolves.toEqual({});
  });

  it("non-ok response => no days", async () => {
    stubFetch(async () => jsonResponse({ detail: "boom" }, 500));

    await expect((await getReturnWindow())()).resolves.toEqual({});
  });

  it("malformed JSON body => no days", async () => {
    stubFetch(
      async () =>
        new Response("<html>not json</html>", {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );

    await expect((await getReturnWindow())()).resolves.toEqual({});
  });

  it("fetch failure (backend down) => no days", async () => {
    stubFetch(async () => {
      throw new Error("ECONNREFUSED");
    });

    await expect((await getReturnWindow())()).resolves.toEqual({});
  });
});
