import type { MetadataRoute } from "next";
import { site } from "@/lib/site";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: "*",
        allow: "/",
        disallow: [
          "/cart",
          "/checkout",
          "/orders",
          "/login",
          "/register",
          "/verify-email",
          "/reset-password",
          "/resend-verification",
          "/forgot-username",
          "/forgot-password",
        ],
      },
    ],
    sitemap: `${site.url}/sitemap.xml`,
  };
}
