import type { Metadata } from "next";
import { Playfair_Display, Plus_Jakarta_Sans } from "next/font/google";
import "./globals.css";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
import { site } from "@/lib/site";
import { DEFAULT_SEED, seedToCssVars } from "@/lib/theme";
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
  // §10.4's measured lever, triggered. §10.4 applies its ~110 KB threshold to
  // the TOTAL `_next/static/media/*.woff2` payload, not to the preloaded
  // subset, and that total is 243,728 B across 12 files in this tree's build —
  // 2.2x the threshold. Measured on this build: the three latin-subset faces
  // are Playfair normal 38,460 B, Playfair italic 38,888 B and Jakarta
  // 27,272 B (104,620 B together), of which exactly two are preloaded.
  //
  // The preloaded critical path is therefore 77,348 B — those two Playfair
  // faces — and protecting that headline is the point of the lever. Jakarta
  // still swaps in on arrival against a metric-adjusted fallback
  // (`src: local(Arial)`, size-adjust 104.98%, ascent-override 98.88%,
  // descent-override 21.15%, all four present in the emitted @font-face), so
  // the delay costs a typeface and no layout shift. The body face is the
  // right thing to de-prioritise: `display: "optional"` is rejected for the
  // display face in §2.2.5 because a late Playfair swap reads as a flash, and
  // that reasoning does not transfer to running text.
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
    <html
      lang="en"
      className={`${playfair.variable} ${jakarta.variable}`}
      // U-W1-T2: the white-label seed layer. DEFAULT_SEED re-states the
      // canonical globals.css token values (#8c5338 / #885035 and the
      // Playfair/Jakarta stacks), so this inline style is a zero visual
      // delta — it only wires the mechanism a merchant theme overrides.
      // The values ride on the root element, where the stylesheet's
      // :root/@theme declarations would resolve, so specificity matches
      // and a seed swap needs no other code. The cast: React's
      // CSSProperties has no --custom-property index, though the runtime
      // accepts it.
      style={seedToCssVars(DEFAULT_SEED) as React.CSSProperties}
    >
      <body className="flex min-h-dvh flex-col bg-paper font-sans text-ink antialiased">
        <Providers>
          <a
            href="#main"
            className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-(--z-skip) focus:bg-ink focus:px-4 focus:py-2 focus:text-sm focus:text-paper"
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
