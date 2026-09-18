import { describe, expect, it } from "vitest";
import {
  filtersToParams,
  filtersToPath,
  isFiltered,
  isInvalidPrice,
  parseFilters,
} from "./filters";

describe("parseFilters", () => {
  it("maps known params", () => {
    const f = parseFilters({
      search: "rose",
      min_price: "100",
      max_price: "5000",
      page: "3",
    });
    expect(f).toEqual({
      search: "rose",
      min_price: "100",
      max_price: "5000",
      page: 3,
    });
  });

  it("trims and drops empty values", () => {
    expect(parseFilters({ search: "  " }).search).toBeUndefined();
    expect(parseFilters({}).search).toBeUndefined();
  });

  it("whitelists orderings — unknown values become undefined", () => {
    expect(parseFilters({ ordering: "-price" }).ordering).toBe("-price");
    expect(parseFilters({ ordering: "DROP TABLE" }).ordering).toBeUndefined();
  });

  it("ignores non-positive / non-numeric pages", () => {
    expect(parseFilters({ page: "1" }).page).toBeUndefined();
    expect(parseFilters({ page: "0" }).page).toBeUndefined();
    expect(parseFilters({ page: "2abc" }).page).toBeUndefined();
    expect(parseFilters({ page: "2" }).page).toBe(2);
  });

  it("takes the first value of repeated params", () => {
    expect(parseFilters({ search: ["a", "b"] }).search).toBe("a");
  });
});

describe("filtersToParams / filtersToPath", () => {
  it("omits the UI-default ordering to keep canonical URLs clean", () => {
    const qs = filtersToParams({ ordering: "-created_at" }).toString();
    expect(qs).toBe("");
  });

  it("round-trips filters to a shareable path", () => {
    expect(filtersToPath({ search: "oudh", page: 2 })).toBe(
      "/products?search=oudh&page=2",
    );
    expect(filtersToPath({})).toBe("/products");
  });
});

describe("isFiltered / isInvalidPrice", () => {
  it("detects narrowing filters (not page/ordering)", () => {
    expect(isFiltered({ search: "x" })).toBe(true);
    expect(isFiltered({ min_price: "1" })).toBe(true);
    expect(isFiltered({ page: 2 })).toBe(false);
    expect(isFiltered({ ordering: "price" })).toBe(false);
  });

  it("flags non-numeric prices", () => {
    expect(isInvalidPrice("abc")).toBe(true);
    expect(isInvalidPrice("12.5")).toBe(false);
    expect(isInvalidPrice(undefined)).toBe(false);
  });
});
