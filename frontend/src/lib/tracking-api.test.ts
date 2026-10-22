import { afterEach, describe, expect, it, vi } from "vitest";
import {
  GUEST_ORDER_TOKEN_HEADER,
  GUEST_TOKEN_MAX_LENGTH,
  TrackingMissError,
  TrackingNetworkError,
  trackShipments,
} from "./tracking-api";
import { getAccessToken } from "./tokens";

vi.mock("./tokens", () => ({ getAccessToken: vi.fn() }));

const ok = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

const err = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });

const shipment = {
  order_number: "ORD-1001",
  status: "in_transit",
  carrier: "Blue Dart",
  tracking_number: "BD123456789",
  estimated_delivery: "2026-10-20T00:00:00+05:30",
  events: [
    { status: "dispatched", occurred_at: "2026-10-15T09:00:00+05:30" },
    { status: "in_transit", occurred_at: "2026-10-16T14:30:00+05:30" },
  ],
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(getAccessToken).mockReset();
});

describe("trackShipments", () => {
  it("GETs the legacy track URL with the guest token header when a token is supplied", async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok({ shipments: [shipment] }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await trackShipments("ORD-1001", "tok-abc");

    expect(result).toEqual([shipment]);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/shipping/track/ORD-1001/");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("include");
    expect(init.headers[GUEST_ORDER_TOKEN_HEADER]).toBe("tok-abc");
    expect(init.headers.Authorization).toBeUndefined();
  });

  it("sends no credential headers when there is no token and no session JWT", async () => {
    vi.mocked(getAccessToken).mockReturnValue(null);
    const fetchMock = vi.fn().mockResolvedValue(ok({ shipments: [] }));
    vi.stubGlobal("fetch", fetchMock);

    await trackShipments("ORD-1001");

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers[GUEST_ORDER_TOKEN_HEADER]).toBeUndefined();
    expect(init.headers.Authorization).toBeUndefined();
  });

  it("falls back to the session JWT as Bearer when no token is supplied", async () => {
    vi.mocked(getAccessToken).mockReturnValue("jwt-xyz");
    const fetchMock = vi.fn().mockResolvedValue(ok({ shipments: [] }));
    vi.stubGlobal("fetch", fetchMock);

    await trackShipments("  ORD-1001  ");

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/shipping/track/ORD-1001/");
    expect(init.headers.Authorization).toBe("Bearer jwt-xyz");
    expect(init.headers[GUEST_ORDER_TOKEN_HEADER]).toBeUndefined();
  });

  it("keeps the backend's guest-token length cap pinned", () => {
    // shipping/views.py GUEST_TOKEN_MAX_LENGTH — drift here is a contract bug.
    expect(GUEST_TOKEN_MAX_LENGTH).toBe(64);
  });

  it("maps the uniform 404 miss to a TrackingMissError with the message verbatim", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(err(404, { error: "Shipment not found" })),
    );

    const e = await trackShipments("ORD-404", "tok").catch((x) => x);
    expect(e).toBeInstanceOf(TrackingMissError);
    expect(e.message).toBe("Shipment not found");
  });

  it("treats every HTTP error status as the same miss class (no case differentiation)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(err(400, { error: "Shipment not found" })),
    );

    const e = await trackShipments("ORD-400", "tok").catch((x) => x);
    expect(e).toBeInstanceOf(TrackingMissError);
    expect(e.message).toBe("Shipment not found");
  });

  it("maps a fetch-level failure to TrackingNetworkError, not a miss", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));

    const e = await trackShipments("ORD-1001", "tok").catch((x) => x);
    expect(e).toBeInstanceOf(TrackingNetworkError);
    expect(e).not.toBeInstanceOf(TrackingMissError);
  });
});
