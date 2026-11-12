import type { PyramidTier } from "@/lib/products-api";

export type { PyramidTier };

/**
 * Data-gated: no tiers means no section at all. Never invent Top/Heart/Base
 * from the free-text prose field.
 */
export default function OlfactoryPyramid({ tiers }: { tiers: PyramidTier[] }) {
  if (tiers.length === 0) return null;
  return (
    <section
      aria-labelledby="olfactory-pyramid-heading"
      className="mt-16 border-t border-hairline pt-8 md:mt-24"
    >
      <p className="text-eyebrow uppercase text-bronze">Olfactory pyramid</p>
      <h2
        id="olfactory-pyramid-heading"
        className="mt-4 font-display text-h2"
      >
        Top, heart and base
      </h2>
      {/* gap-px over the hairline colour draws the tier rules themselves. */}
      <div className="mt-8 grid gap-px bg-hairline md:grid-cols-3">
        {tiers.map((tier) => (
          <div key={tier.key} className="bg-surface p-6 md:p-8">
            <p className="text-eyebrow uppercase text-ink-secondary">
              {tier.index} · {tier.label}
            </p>
            <h3 className="mt-2 font-display text-h3">{tier.label}</h3>
            <ul className="mt-6 space-y-5 border-t border-hairline pt-6">
              {tier.notes.map((note) => (
                <li key={note.name}>
                  <span className="block text-body text-ink">{note.name}</span>
                  {note.detail ? (
                    <span className="mt-1 block text-small text-ink-secondary">
                      {note.detail}
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}
