/**
 * The listing skeleton. §6: it mirrors the real grid's columns at every
 * breakpoint — a skeleton of a different shape causes the CLS it exists to
 * prevent — and it stays inside §3's hard cap of 12 shimmering instances per
 * page. Each card shimmers 3 blocks (image + two text bars), so the default
 * `count = 4` is 4 × 3 = 12 — exactly at the cap, which is why the listing
 * does not skeleton all 12 of its products (that would be 36 instances).
 *
 * `gridClassName` is the route's own column/gutter string so the fallback and
 * the real grid are one declaration. The default mirrors the home
 * New-arrivals grid's SHAPE — its columns (`grid-cols-2 sm:grid-cols-3
 * lg:grid-cols-4`) and its `gap-x-4` are `app/page.tsx:35` exactly — but its
 * row gap is NOT what home ships: the default carries `gap-y-12` (48px) while
 * home ships `gap-y-8` (32px). (This comment used to claim the two strings
 * were verbatim identical; the wave-6 audit corrected the claim — comment
 * only, the default itself is deliberately unchanged in this pass.) The
 * row-gap drift, along with home's `sm:grid-cols-3` where §4 keeps two
 * columns on `sm`, is §5.1's to reconcile when home adopts the §5.2
 * 2/2/3/4 string; until then the skeleton's job is to match the grid above
 * it, and the default's own 48px row gap — over P1's 32px inside-section
 * cap — travels with home's row gap untouched. The listing passes its own
 * §5.2 2/2/3/4 string explicitly below; when home adopts it, this default
 * becomes that string and the prop can go.
 */
export default function GridSkeleton({
  count = 4,
  gridClassName = "grid grid-cols-2 gap-x-4 gap-y-12 sm:grid-cols-3 lg:grid-cols-4",
}: {
  count?: number;
  gridClassName?: string;
}) {
  return (
    <div aria-hidden="true" className={gridClassName}>
      {Array.from({ length: count }, (_, i) => (
        <div key={i}>
          <div className="skeleton aspect-[4/5]" />
          <div className="skeleton mt-3 h-4 w-2/3" />
          <div className="skeleton mt-2 h-3 w-1/3" />
        </div>
      ))}
    </div>
  );
}
