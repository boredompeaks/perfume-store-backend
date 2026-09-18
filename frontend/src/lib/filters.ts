export const ORDERINGS = [
  "price",
  "-price",
  "name",
  "-name",
  "created_at",
  "-created_at",
] as const;

export type Ordering = (typeof ORDERINGS)[number];

/** UI default when the URL carries no ordering (the backend has none — F-12). */
export const DEFAULT_ORDERING: Ordering = "-created_at";

export type ProductFilters = {
  search?: string;
  category?: string;
  min_price?: string;
  max_price?: string;
  ordering?: Ordering;
  page?: number;
};

type SearchParams = Record<string, string | string[] | undefined>;

export function parseFilters(sp: SearchParams): ProductFilters {
  const get = (key: string): string | undefined => {
    const value = sp[key];
    const raw = Array.isArray(value) ? value[0] : value;
    const trimmed = raw?.trim();
    return trimmed ? trimmed : undefined;
  };

  const pageRaw = get("page");
  const page =
    pageRaw && /^\d+$/.test(pageRaw) && Number(pageRaw) > 1
      ? Number(pageRaw)
      : undefined;

  const orderingRaw = get("ordering");
  const ordering = (ORDERINGS as readonly string[]).includes(
    orderingRaw ?? "",
  )
    ? (orderingRaw as Ordering)
    : undefined;

  return {
    search: get("search"),
    category: get("category"),
    min_price: get("min_price"),
    max_price: get("max_price"),
    ordering,
    page,
  };
}

export function filtersToParams(filters: ProductFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.search) params.set("search", filters.search);
  if (filters.category) params.set("category", filters.category);
  if (filters.min_price) params.set("min_price", filters.min_price);
  if (filters.max_price) params.set("max_price", filters.max_price);
  if (filters.ordering && filters.ordering !== DEFAULT_ORDERING) {
    params.set("ordering", filters.ordering);
  }
  if (filters.page) params.set("page", String(filters.page));
  return params;
}

export function filtersToPath(filters: ProductFilters): string {
  const qs = filtersToParams(filters).toString();
  return qs ? `/products?${qs}` : "/products";
}

/** Filters that narrow the listing (anything but page/ordering). */
export function isFiltered(filters: ProductFilters): boolean {
  return Boolean(
    filters.search || filters.category || filters.min_price || filters.max_price,
  );
}

export function isInvalidPrice(value: string | undefined): boolean {
  if (value === undefined) return false;
  return Number.isNaN(Number(value));
}
