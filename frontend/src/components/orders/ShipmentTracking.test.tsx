import { describe, expect, it } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { safeExternalUrl } from "@/lib/shipping-api";
import type { Shipment } from "@/lib/tracking-api";
import ShipmentTracking from "./ShipmentTracking";

const ORDER = "ORD-2026-ABCDEF12";

function shipment(overrides: Partial<Shipment> = {}): Shipment {
  return {
    order_number: ORDER,
    status: "in_transit",
    carrier: "BlueDart",
    provider: "shiprocket",
    tracking_number: "BD123456",
    tracking_url: "https://track.example.com/BD123456",
    estimated_delivery: "2026-11-02T00:00:00Z",
    events: [
      { status: "label_created", occurred_at: "2026-10-28T09:00:00Z" },
      { status: "dispatched", occurred_at: "2026-10-29T11:30:00Z" },
    ],
    ...overrides,
  };
}

function renderWith(shipments: Shipment[]): string {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
    },
  });
  client.setQueryData(["orders", "tracking", ORDER], shipments);
  return renderToStaticMarkup(
    <QueryClientProvider client={client}>
      <ShipmentTracking orderNumber={ORDER} />
    </QueryClientProvider>,
  );
}

describe("ShipmentTracking", () => {
  it("renders exactly one tracking panel and one parcel", () => {
    const html = renderWith([shipment()]);

    expect((html.match(/data-testid="shipment-tracking"/g) ?? []).length).toBe(
      1,
    );
    expect((html.match(/data-testid="shipment-parcel"/g) ?? []).length).toBe(1);
  });

  it("renders status, carrier, provider, waybill and timeline", () => {
    const html = renderWith([shipment()]);

    expect(html).toContain("In transit");
    expect(html).toContain("BlueDart");
    expect(html).toContain("via shiprocket");
    expect(html).toContain("BD123456");
    expect(html).toContain("Checkpoints");
    expect(html).toContain("Label created");
    expect(html).toContain("Dispatched");
    expect(html.indexOf("Label created")).toBeLessThan(
      html.indexOf("Dispatched"),
    );
  });

  it("omits the provider line when the parcel is unlinked", () => {
    const html = renderWith([shipment({ provider: null })]);

    expect(html).not.toContain("via ");
    expect(html).toContain("BD123456");
  });

  it("renders the tracking link as a safe external tab", () => {
    const html = renderWith([shipment()]);

    expect(html).toContain('href="https://track.example.com/BD123456"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain("Track this parcel");
  });

  it("never renders an anchor for a non-http(s) tracking URL", () => {
    for (const unsafe of [
      "javascript:alert(1)",
      "data:text/html,hi",
      "//evil.example/steal",
    ]) {
      const html = renderWith([shipment({ tracking_url: unsafe })]);

      expect(html).not.toContain("<a ");
      expect(html).not.toContain("javascript:");
      // The parcel itself still renders; only the link is withheld.
      expect(html).toContain("BD123456");
    }
  });

  it("shows the empty state when the order has no parcels", () => {
    const html = renderWith([]);

    expect(html).toContain("Tracking will appear here once your order ships.");
    expect(html).not.toContain('data-testid="shipment-parcel"');
  });
});

describe("safeExternalUrl", () => {
  it("admits only absolute http(s) URLs", () => {
    expect(safeExternalUrl("https://track.example.com/x")).toBe(
      "https://track.example.com/x",
    );
    expect(safeExternalUrl("http://track.example.com/x")).toBe(
      "http://track.example.com/x",
    );
  });

  it("refuses every other scheme, malformed or empty value", () => {
    for (const raw of [
      "javascript:alert(1)",
      "data:text/html,hi",
      "ftp://example.com/x",
      "//evil.example/x",
      "not a url",
      "",
      null,
      undefined,
    ]) {
      expect(safeExternalUrl(raw)).toBeNull();
    }
  });
});
