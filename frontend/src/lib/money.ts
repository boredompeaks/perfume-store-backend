const formatter = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
});

/**
 * Display-only formatting of server-provided money strings ("1234.00").
 * All pricing/truth is server-computed; this never does arithmetic.
 */
export function formatINR(value: string | number): string {
  const n = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(n)) return String(value);
  return formatter.format(n);
}
