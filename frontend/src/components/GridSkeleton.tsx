/**
 * The listing skeleton. §6: it mirrors the real grid's columns at every
 * breakpoint — a skeleton of a different shape causes the CLS it exists to
 * prevent — and it stays inside §3's hard cap of 12 shimmering instances per
 * page. Each card shimmers 3 blocks (image + two text bars), so the default
 * `count = 4` is 4 × 3 = 12 — exactly at the cap, which is why the listing
 * does not skeleton all 12 of its products (that would be 36 instances).
 *
 * `gridClassName` is the route's own column/gutter string so the fallback and
 * the real grid are one declaration. The default is the string the home page's
 * New-arrivals grid ships verbatim (`grid-cols-2 gap-x-4 gap-y-12
 * sm:grid-cols-3 lg:grid-cols-4`) — the skeleton's whole job is to match the
 * grid it stands in for, so it inherits home's `sm:grid-cols-3` and its 48px
 * row gap as-is even though §4 keeps the product grid at two columns on `sm`
 * and P1 caps inside-section gaps at 32px; those two are §5.1's to fix, and
 * changing the default here would make the skeleton LIE about the grid above
 * it. The listing passes its own §5.2 2/2/3/4 string explicitly below; when
 * home adopts it, this default becomes that string and the prop can go.
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
