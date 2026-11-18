import type { MetadataRoute } from "next";
import { site } from "@/lib/site";
import { disallowedPaths } from "@/lib/pages";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: "*",
        allow: "/",
        // Registry-derived, so robots can never drift from the sitemap's set.
        disallow: disallowedPaths(),
      },
    ],
    sitemap: `${site.url}/sitemap.xml`,
  };
}
