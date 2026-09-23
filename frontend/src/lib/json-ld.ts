/**
 * Script-context escaping for JSON-LD payloads [SPEC-17-06, R-17.17].
 *
 * JSON.stringify output is NOT safe to embed inside a <script> tag:
 * a literal "</script>" (or "<!--", or the line separators U+2028/2029)
 * inside any string value can close the element early or break JS
 * parsing, letting staff-controlled product text execute markup in the
 * page. Escaping these characters as \uXXXX keeps the JSON semantically
 * identical (JSON.parse decodes the escapes back to the same string) but
 * makes it impossible for the payload to terminate the script element.
 */
const SCRIPT_CONTEXT_ESCAPES: Record<string, string> = {
  "<": "\\u003c",
  ">": "\\u003e",
  "&": "\\u0026",
  "\u2028": "\\u2028",
  "\u2029": "\\u2029",
};

const SCRIPT_CONTEXT_ESCAPE_RE = /[<>&\u2028\u2029]/g;

export function toJsonLdScriptContent(data: unknown): string {
  return JSON.stringify(data).replace(
    SCRIPT_CONTEXT_ESCAPE_RE,
    (char) => SCRIPT_CONTEXT_ESCAPES[char],
  );
}
