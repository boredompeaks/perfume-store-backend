/**
 * `tracking_url` is operator-configured, so scheme-check it rather than trust
 * React with an href. `new URL` also rejects malformed and protocol-relative
 * values; anything not absolute http(s) is not a link.
 */
export function safeExternalUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const parsed = new URL(raw);
    return parsed.protocol === "http:" || parsed.protocol === "https:"
      ? parsed.toString()
      : null;
  } catch {
    return null;
  }
}
