import { describe, expect, it } from "vitest";
import { formatINR } from "./money";

describe("formatINR", () => {
  it("formats API money strings with Indian digit grouping", () => {
    expect(formatINR("1800")).toBe("₹1,800.00");
    expect(formatINR("2000.00")).toBe("₹2,000.00");
    expect(formatINR("1199999")).toBe("₹11,99,999.00");
  });

  it("keeps paise", () => {
    expect(formatINR("1199.5")).toBe("₹1,199.50");
    expect(formatINR("0.05")).toBe("₹0.05");
  });

  it("accepts numbers (display arithmetic results only)", () => {
    expect(formatINR(0)).toBe("₹0.00");
  });

  it("never invents money — garbage in, garbage out", () => {
    expect(formatINR("not-a-price")).toBe("not-a-price");
  });
});
