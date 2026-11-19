import { describe, expect, it, vi } from "vitest";
import nextConfig from "../../next.config.mjs";

type SecurityHeader = { key: string; value: string };
type SecurityConfig = {
  poweredByHeader?: boolean;
  headers: () => Promise<{ headers: SecurityHeader[] }[]>;
};

async function securityHeaders(
  config = nextConfig as unknown as SecurityConfig,
): Promise<SecurityHeader[]> {
  const [{ headers }] = await config.headers();
  return headers;
}

function headerValue(headers: SecurityHeader[], key: string): string | undefined {
  return headers.find((header) => header.key === key)?.value;
}

function directive(csp: string, name: string): string {
  return (
    csp
      .split(";")
      .map((part) => part.trim())
      .find((part) => part.split(" ")[0] === name) ?? ""
  );
}

describe("security headers", () => {
  it("test_csp_has_no_unsafe_inline_or_eval_and_keeps_required_origins", async () => {
    const csp = headerValue(await securityHeaders(), "Content-Security-Policy");
    expect(csp).toBeDefined();
    expect(csp).not.toContain("'unsafe-inline'");
    expect(csp).not.toContain("'unsafe-eval'");
    expect(directive(csp!, "script-src")).toContain(
      "https://checkout.razorpay.com",
    );
    expect(directive(csp!, "frame-src")).toContain(
      "https://api.razorpay.com",
    );
    expect(directive(csp!, "connect-src")).toContain(
      "https://api.razorpay.com",
    );
    expect(directive(csp!, "connect-src")).toContain(
      "https://lumberjack.razorpay.com",
    );

    const saved = process.env.NEXT_PUBLIC_SENTRY_DSN;
    process.env.NEXT_PUBLIC_SENTRY_DSN =
      "https://public@o1.ingest.sentry.io/42";
    try {
      vi.resetModules();
      const config = (await import("../../next.config.mjs"))
        .default as unknown as SecurityConfig;
      const sentryCsp = headerValue(
        await securityHeaders(config),
        "Content-Security-Policy",
      );
      expect(sentryCsp).not.toContain("'unsafe-inline'");
      expect(directive(sentryCsp!, "connect-src")).toContain(
        "https://o1.ingest.sentry.io",
      );
    } finally {
      if (saved === undefined) {
        delete process.env.NEXT_PUBLIC_SENTRY_DSN;
      } else {
        process.env.NEXT_PUBLIC_SENTRY_DSN = saved;
      }
    }
  });

  it("suppresses the X-Powered-By banner", () => {
    expect(nextConfig.poweredByHeader).toBe(false);
  });

  it("sends COOP and CORP and omits COEP for the gateway and Sentry", async () => {
    const headers = await securityHeaders();
    expect(headerValue(headers, "Cross-Origin-Opener-Policy")).toBe(
      "same-origin-allow-popups",
    );
    expect(headerValue(headers, "Cross-Origin-Resource-Policy")).toBe(
      "same-origin",
    );
    expect(headerValue(headers, "Cross-Origin-Embedder-Policy")).toBeUndefined();
  });
});
