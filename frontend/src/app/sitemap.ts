import type { MetadataRoute } from "next";
import { API_BASE } from "@/lib/config";
import { site } from "@/lib/site";
import { pages } from "@/lib/pages";
import type { ProductPage } from "@/lib/types";

// Regenerated hourly. NOTE: walking every product page costs ~count/12
// requests at the backend's default PRODUCTS_PAGE_SIZE of 12: an
// env-overridable setting (backend/config/settings.py:978), consumed by the
// Paginator in backend/products/views.py:108, not a hardcoded constant.
export const revalidate = 3600;

// Static-route priorities: home leads, the shop listing is close behind, and
// supporting/legal pages sit lower. Product slugs are ranked below the shop
// index itself.
function staticPriority(path: string): number {
  if (path === "/") return 1;
  if (path === "/products") return 0.9;
  return 0.5;
}

function staticChangeFrequency(path: string): "weekly" | "monthly" {
  if (path === "/" || path === "/products") return "weekly";
  return "monthly";
}

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  // Static entries are generated from the page registry so sitemap and robots
  // can never disagree about which routes exist or are indexable.
  const staticRoutes: MetadataRoute.Sitemap = pages
    .filter((p) => p.indexable)
    .map((p) => ({
      url: `${site.url}${p.path}`,
      lastModified: new Date(),
      changeFrequency: staticChangeFrequency(p.path),
      priority: staticPriority(p.path),
    }));

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
