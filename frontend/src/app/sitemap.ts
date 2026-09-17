import type { MetadataRoute } from "next";
import { API_BASE } from "@/lib/config";
import { site } from "@/lib/site";
import type { ProductPage } from "@/lib/types";

// Regenerated hourly. NOTE: walking every page at the backend's current
// hardcoded page size of 2 costs ~count/2 requests — a temporary cost that
// collapses once the P0 page-size fix lands (BACKEND_REQUESTS.md).
export const revalidate = 3600;

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const staticRoutes: MetadataRoute.Sitemap = [
    {
      url: `${site.url}/`,
      lastModified: new Date(),
      changeFrequency: "weekly",
      priority: 1,
    },
    {
      url: `${site.url}/products`,
      lastModified: new Date(),
      changeFrequency: "weekly",
      priority: 0.9,
    },
  ];

  try {
    const productRoutes: MetadataRoute.Sitemap = [];
    let page = 1;
    let totalPages = 1;
    do {
      const res = await fetch(`${API_BASE}/api/products/?page=${page}`, {
        headers: { Accept: "application/json" },
      });
      if (!res.ok) break;
      const data = (await res.json()) as ProductPage;
      for (const product of data.results) {
        productRoutes.push({
          url: `${site.url}/products/${product.slug}`,
          lastModified: new Date(product.created_at),
          changeFrequency: "weekly",
          priority: 0.8,
        });
      }
      totalPages = data.total_pages;
      page += 1;
    } while (page <= totalPages);
    return [...staticRoutes, ...productRoutes];
  } catch {
    // Backend unreachable — ship the static routes rather than failing.
    return staticRoutes;
  }
}
