const formatter = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
});

/** Display-only: server-provided money strings, never arithmetic. */
export function formatINR(value: string | number): string {
  const n = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(n)) return String(value);
  return formatter.format(n);
}
