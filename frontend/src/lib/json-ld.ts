/**
 * JSON.stringify output is unsafe inside `<script>`: a literal `</script>`,
 * `<!--` or U+2028/2029 can close the element or break parsing. Escaping them
 * as \uXXXX keeps the JSON semantically identical.
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
