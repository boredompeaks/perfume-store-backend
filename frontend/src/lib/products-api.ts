import { apiFetch } from "./api";
import type { Product, ProductPage } from "./types";

export function fetchProductsPage(
  params: URLSearchParams,
): Promise<ProductPage> {
  return apiFetch<ProductPage>(`/api/products/?${params.toString()}`, {
    revalidate: 60,
  });
}

export function fetchProduct(slug: string): Promise<Product> {
  return apiFetch<Product>(`/api/products/${slug}/`, { revalidate: 60 });
}
