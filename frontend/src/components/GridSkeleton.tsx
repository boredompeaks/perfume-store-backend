/**
 * Must mirror the real grid's columns or it causes the CLS it prevents.
 * Each card is 3 blocks, so the default count stays within the 12-block cap.
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
          <div className="skeleton aspect-[3/4]" />
          <div className="skeleton mt-3 h-4 w-2/3" />
          <div className="skeleton mt-2 h-3 w-1/3" />
        </div>
      ))}
    </div>
  );
}
