/**
 * Sanitize a `?next=` redirect target. Internal absolute paths only.
 * Blocks: absolute URLs (https://…), protocol-relative (//…), and backslash
 * tricks (/\…) that browsers normalize into protocol-relative form.
 */
export function safeNext(raw: string | undefined | null): string | undefined {
  if (!raw) return undefined;
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.includes("\\")) {
    return undefined;
  }
  return raw;
}
