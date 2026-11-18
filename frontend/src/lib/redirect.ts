/**
 * Only internal absolute paths: blocks absolute URLs, protocol-relative `//`
 * and backslashes that browsers normalize into `//`.
 */
export function safeNext(raw: string | undefined | null): string | undefined {
  if (!raw) return undefined;
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.includes("\\")) {
    return undefined;
  }
  return raw;
}
