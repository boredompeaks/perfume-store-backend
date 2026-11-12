import type { PyramidTier } from "@/lib/products-api";

export type { PyramidTier };

/**
 * Universal fragrance guidance only: product-specific claims have no API
 * field, and the copy pins forbid digits and em-dashes in any body string.
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
