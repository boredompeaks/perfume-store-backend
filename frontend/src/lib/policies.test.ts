import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
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

/**
 * Dispatch/delivery windows (same A-12.D.2 single-source rule as the return
 * window): the shipping page is imported beside `shippingPolicy`, so "the
 * page renders the constants" is proven on both sides — the module's strings
 * against the element tree the page actually returns, plus a source check
 * that no window range is typed into the page again. A page that re-wrote
 * the numbers itself would satisfy a rendered-text check by coincidence;
 * only the source check fails it.
 */
function collectText(node: unknown): string {
  if (node === null || node === undefined || typeof node === "boolean") {
    return "";
  }
  if (typeof node === "string" || typeof node === "number") {
    return String(node);
  }
  if (Array.isArray(node)) {
    return node.map(collectText).join("");
  }
  if (typeof node === "object" && "props" in node) {
    return collectText(
      (node as { props?: { children?: unknown } }).props?.children,
    );
  }
  return "";
}

describe("shippingPolicy", () => {
  it("states both windows: non-empty, each carrying a number", async () => {
    const { shippingPolicy } = await import("./policies");

    expect(shippingPolicy.dispatchWindow.trim()).not.toBe("");
    expect(shippingPolicy.deliveryWindow.trim()).not.toBe("");
    expect(shippingPolicy.dispatchWindow).toMatch(/\d/);
    expect(shippingPolicy.deliveryWindow).toMatch(/\d/);
  });

  it("the shipping page renders exactly these strings (import both sides)", async () => {
    const [{ shippingPolicy }, { default: ShippingPage }] = await Promise.all([
      import("./policies"),
      import("@/app/shipping/page"),
    ]);

    const text = collectText(ShippingPage());
    // The whole sentence, word for word — structure is the page's contract,
    // the numbers come from the module, so nothing may drift between them.
    const sentence =
      `Orders are typically dispatched within ${shippingPolicy.dispatchWindow} ` +
      `of payment confirmation, and delivery usually takes a further ` +
      `${shippingPolicy.deliveryWindow} depending on your location. ` +
      `Business days exclude Sundays and public holidays.`;
    expect(text).toContain(sentence);
    // §5.7 (D1 screens): the same two module windows ALSO render as the
    // scannable fact matrix at the top, so each appears twice on screen
    // (facts + prose) by design. The pin is the SET of range claims: a
    // third distinct window typed anywhere on the page fails here, and a
    // typed range in the source fails the source pin below.
    const claims = text.match(/\d+[–-]\d+ (?:business )?days/g) ?? [];
    expect([...new Set(claims)].sort()).toEqual(
      [shippingPolicy.dispatchWindow, shippingPolicy.deliveryWindow].sort(),
    );
  });

  it("states each window twice: the top fact matrix and the prose sentence", async () => {
    const [{ shippingPolicy }, { default: ShippingPage }] = await Promise.all([
      import("./policies"),
      import("@/app/shipping/page"),
    ]);

    const text = collectText(ShippingPage());
    // Counting, not just containment: a refactor that drops the fact
    // matrix (or the sentence) leaves only one occurrence and fails here.
    const occurrences = (needle: string) => text.split(needle).length - 1;
    expect(occurrences(shippingPolicy.dispatchWindow)).toBe(2);
    expect(occurrences(shippingPolicy.deliveryWindow)).toBe(2);
  });

  it("no window range is typed into the page source — constants only", () => {
    const source = readFileSync(
      fileURLToPath(new URL("../app/shipping/page.tsx", import.meta.url)),
      "utf8",
    );

    expect(source).toContain("shippingPolicy.dispatchWindow");
    expect(source).toContain("shippingPolicy.deliveryWindow");
    expect(source).not.toMatch(/\d+[–-]\d+/);
  });
});

/**
 * /returns (D1 screens, U-W13-T12): the API window renders in the top
 * fact matrix AND the short-version prose when `GET /api/settings/`
 * publishes one, and the page ships NO return-window count when it does
 * not: A-12.D.2's single-source rule extended to the new fact-matrix
 * surface. (Operational copy, i.e. refund timing and the damage-notice
 * deadline, carries its own figures by design and is not the window.)
 */
describe("returns page window claims (A-12.D.2)", () => {
  async function renderReturns() {
    const { default: ReturnsPage } = await import("@/app/returns/page");
    return collectText(await ReturnsPage());
  }

  it("with a published window: fact matrix and prose both state it", async () => {
    stubFetch(async () =>
      jsonResponse({ support_email: "", return_window_days: 14 }),
    );

    const text = await renderReturns();
    expect(text).toContain("within 14 days of delivery");
    expect(text.split("14 days from delivery").length - 1).toBe(1);
  });

  it("with no published window: no return-window count typed into the page", async () => {
    stubFetch(async () => jsonResponse({ support_email: "" }));

    const text = await renderReturns();
    expect(text).not.toMatch(/\d+ days/);
  });
});
