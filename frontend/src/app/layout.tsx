import type { Metadata } from "next";
import { Playfair_Display, Plus_Jakarta_Sans } from "next/font/google";
import "./globals.css";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
import { site } from "@/lib/site";
import { Providers } from "./providers";

/**
 * §2.2 — the pairing is Playfair Display (display) + Plus Jakarta Sans
 * (body / UI / eyebrows), replacing Fraunces + Source Sans 3. Playfair is the
 * canonical web face of the transitional-serif register the refs' DESIGN.md
 * describes ("terminal swashes and distinct transitional contrast… copperplate
 * printing") and is used as a display face only — it never sets a body
 * paragraph. Jakarta has the tall x-height and unambiguous `l` / `1` / `I` that
 * a filter field and a tabular price need.
 *
 * §2.2.5 loading strategy: `next/font/google` resolves at BUILD time and
 * serves local `.woff2` only. None of the 17 refs' `fonts.googleapis.com` or
 * Material Symbols requests is permitted (`font-src 'self'` in the CSP), and no
 * `preconnect` is added because after `next/font` there is no third-party font
 * request to preconnect to.
 *
 * `subsets: ["latin"]` — the store is English-language; the refs are
 * French/English mixed and French copy needs no glyphs beyond latin.
 *
 * `display: "swap"` — `swap` means a period of fallback rendering, held near
 * CLS 0 by the metric-adjusted fallback face `next/font` generates. That
 * matters more here than it would for two similar faces: a didone-adjacent
 * transitional serif and a geometric-humanist sans are not metric-similar, so
 * `adjustFontFallback` is left at its default (true) rather than disabled.
 * `display: "optional"` is explicitly rejected — it would let the Playfair
 * headline swap in late and read as a flash.
 *
 * Both stay VARIABLE (one file per face rather than one per weight). Playfair
 * is requested with its `ital` axis because the home hero sets one italic word
 * in the display face; Jakarta is `wght` only, as §2.2.5 specifies — nothing
 * sets Jakarta in italic, and a second icon webfont is not permitted.
 */
const playfair = Playfair_Display({
  subsets: ["latin"],
  style: ["normal", "italic"],
  variable: "--font-playfair",
  display: "swap",
});

const jakarta = Plus_Jakarta_Sans({
  subsets: ["latin"],
  variable: "--font-jakarta",
  display: "swap",
  // §10.4's measured lever, triggered. `_next/static/media/*.woff2` totals
  // 243,728 B with a 104,620 B preloaded critical path (Playfair normal
  // 38,460 + Playfair italic 38,888 + Jakarta 27,272) — over the ~110 KB
  // threshold §10.4 names. Its stated remedy is `preload: false` here, to
  // protect the Playfair headline. Jakarta still swaps in on arrival against
  // a metric-adjusted fallback (`src: local(Arial)`, size-adjust 104.98%,
  // ascent-override 98.88%, descent-override 21.15%), so the delay costs a
  // typeface and no layout shift. The body face is the right thing to
  // de-prioritise: `display: "optional"` is rejected for the display face in
  // §2.2.5 because a late Playfair swap reads as a flash, and that reasoning
  // does not transfer to running text.
  preload: false,
});

export const metadata: Metadata = {
  metadataBase: new URL(site.url),
  title: {
    default: `${site.name} — ${site.tagline}`,
    template: `%s — ${site.name}`,
  },
  description: site.description,
  openGraph: {
    type: "website",
    siteName: site.name,
    url: site.url,
    title: `${site.name} — ${site.tagline}`,
    description: site.description,
  },
  twitter: {
    card: "summary_large_image",
    title: `${site.name} — ${site.tagline}`,
    description: site.description,
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={`${playfair.variable} ${jakarta.variable}`}>
      <body className="flex min-h-dvh flex-col bg-paper font-sans text-ink antialiased">
        <Providers>
          <a
            href="#main"
            className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:bg-ink focus:px-4 focus:py-2 focus:text-sm focus:text-paper"
          >
            Skip to content
          </a>
          <Header />
          <main id="main" className="flex-1">
            {children}
          </main>
          <Footer />
        </Providers>
      </body>
    </html>
  );
}
