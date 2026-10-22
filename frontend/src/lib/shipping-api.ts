/**
 * The only URLs the panel will turn into a link.
 *
 * `tracking_url` is operator-configured data (a provider template), so it is
 * untrusted at render time: scheme-check it here and never rely on React to
 * neutralise `javascript:` in an href. Anything that is not an absolute
 * http(s) URL is not a link at all, which is why `new URL` is used rather
 * than a prefix test (it rejects malformed and protocol-relative values).
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
