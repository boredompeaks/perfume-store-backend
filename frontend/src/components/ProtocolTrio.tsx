import type { PyramidTier } from "@/lib/products-api";

export type { PyramidTier };

/**
 * Wear & care protocol trio (design screen
 * `ther_s_culaire…refined_hero_flow`, block "WEAR GUIDANCE & TECHNICAL
 * PROTOCOLS"): three hairline cards — Application, Longevity & Sillage,
 * Maceration & Care — built U-W15-T18 as the PDP slice T6 deferred.
 *
 * Layout is the screen's; the copy is universal fragrance-knowledge
 * guidance only. The screen's product-specific claims have no field
 * behind them and are removed under P5 (spec §0b.3 placeholder table and
 * §12.D: "tenacity approximately 14–18 hours", the extrait-concentration
 * sentence, "macerated for four months in silence at our Grasse
 * atelier", the 15°C–21°C range). The copy pins in lib/pdp.test.ts
 * enforce that no digit and no em-dash ships in any body string, so a
 * future edit cannot smuggle one of those numbers back in.
 *
 * Static by design: this is reading content, and the page's one accent
 * moment is the story drift on the philosophy figure (P2). Same
 * partition system as the olfactory pyramid — gap-px over the hairline
 * colour draws the card rules; no shadows, no radius, one surface.
 */
const PROTOCOLS = [
  {
    key: "application",
    eyebrow: "Protocol",
    title: "Application",
    body: "Spray once onto pulse points from about twenty centimetres away, then let it dry on the skin. Rubbing wrists together flattens the opening.",
  },
  {
    key: "wear",
    eyebrow: "Wear",
    title: "Longevity & Sillage",
    body: "How long a fragrance lasts, and how far its sillage carries, depends on skin chemistry, climate and where it is applied. Warm skin reads stronger; dry skin fades sooner.",
  },
  {
    key: "conservation",
    eyebrow: "Conservation",
    title: "Maceration & Care",
    body: "Fragrance keeps best away from direct sunlight and sudden heat, which degrade both colour and scent. Keep the cap seated after each wear.",
  },
] as const;

export default function ProtocolTrio() {
  return (
    <section
      aria-label="Wear and care protocol"
      className="mt-16 border-t border-hairline pt-8 md:mt-24 lg:mt-32"
    >
      <div className="grid gap-px bg-hairline md:grid-cols-3">
        {PROTOCOLS.map((card) => (
          <article key={card.key} className="bg-surface p-6 md:p-8">
            <p className="text-eyebrow uppercase text-bronze">{card.eyebrow}</p>
            <h3 className="mt-2 font-display text-h3">{card.title}</h3>
            <p className="mt-4 text-small text-ink-secondary">{card.body}</p>
          </article>
        ))}
      </div>
    </section>
  );
}
