import type { MetadataRoute } from "next";
import { site } from "@/lib/site";
import { disallowedPaths } from "@/lib/pages";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: "*",
        allow: "/",
        // Derived from the page registry: every non-indexable static route is
        // disallowed, so robots can never drift from the sitemap's indexable set.
        disallow: disallowedPaths(),
      },
    ],
    sitemap: `${site.url}/sitemap.xml`,
  };
}
