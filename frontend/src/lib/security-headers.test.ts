import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it } from "vitest";
import nextConfig from "../../next.config.mjs";

type SecurityHeader = { key: string; value: string };
type SecurityConfig = {
  poweredByHeader?: boolean;
  headers: () => Promise<{ headers: SecurityHeader[] }[]>;
};
type CspModule = {
  SENSITIVE_PREFIXES: readonly string[];
  CSP_ENFORCEMENT_ENV: string;
  CSP_MODE_DEFAULT: string;
  enforceFor: (pathname: string, mode?: string) => string;
  buildCsp: (nonce: string) => string;
  cspHeaderName: (mode: string) => string;
};
type ProxyModule = {
  proxy: (request: NextRequest) => Response;
  config: {
    matcher: {
      source: string;
      missing?: { type: string; key: string; value?: string }[];
    }[];
  };
};

const cspPath = fileURLToPath(new URL("./csp.ts", import.meta.url));
const proxyPath = fileURLToPath(new URL("../proxy.ts", import.meta.url));
const SENSITIVE = [
  "login",
  "register",
  "forgot-password",
  "forgot-username",
  "reset-password",
  "verify-email",
  "resend-verification",
  "checkout",
  "orders",
];
const STATIC_ROUTES = [
  "/",
  "/products",
  "/products/example",
  "/cart",
  "/contact",
  "/privacy",
  "/terms",
  "/returns",
  "/shipping",
  "/track-order",
];
const SWITCH = "CSP_ENFORCEMENT";

const policyOf = (response: Response, name: string): string =>
  response.headers.get(name) ?? "";
const nonceOf = (csp: string): string | undefined =>
  csp.match(/'nonce-([^']+)'/)?.[1];
const directive = (csp: string, name: string): string =>
  csp
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.split(" ")[0] === name) ?? "";

async function loadCsp(): Promise<CspModule> {
  return (await import("./csp")) as unknown as CspModule;
}

async function loadProxy(): Promise<ProxyModule> {
  return (await import("../proxy")) as unknown as ProxyModule;
}

async function securityHeaders(
  config = nextConfig as unknown as SecurityConfig,
): Promise<SecurityHeader[]> {
  const [{ headers }] = await config.headers();
  return headers;
}

function headerValue(headers: SecurityHeader[], key: string): string | undefined {
  return headers.find((header) => header.key === key)?.value;
}

beforeEach(() => {
  delete process.env[SWITCH];
});

describe("storefront CSP", () => {
  it("classifies the nine interaction prefixes and keeps the rest report-only", async () => {
    expect(existsSync(cspPath)).toBe(true);
    const { SENSITIVE_PREFIXES, enforceFor, buildCsp } = await loadCsp();

    expect([...SENSITIVE_PREFIXES]).toEqual(SENSITIVE);
    for (const prefix of SENSITIVE) {
      expect(enforceFor(`/${prefix}`, "enforce")).toBe("enforce");
      expect(enforceFor(`/${prefix}/step`, "enforce")).toBe("enforce");
      expect(enforceFor(`/${prefix}/`, "enforce")).toBe("enforce");
      expect(enforceFor(`/${prefix}`, "report")).toBe("report");
    }
    for (const path of STATIC_ROUTES) {
      expect(enforceFor(path, "enforce")).toBe("report");
    }
    expect(enforceFor("/login")).toBe("report");
    expect(enforceFor("/loginx", "enforce")).toBe("report");

    const csp = buildCsp("test-nonce");
    expect(directive(csp, "script-src")).toContain("'nonce-test-nonce'");
    expect(directive(csp, "script-src")).not.toContain("'unsafe-inline'");
    expect(directive(csp, "script-src")).not.toContain("'unsafe-eval'");
    expect(directive(csp, "script-src")).toContain(
      "https://checkout.razorpay.com",
    );
    expect(directive(csp, "style-src")).toContain("'nonce-test-nonce'");
    expect(directive(csp, "style-src")).not.toContain("'unsafe-inline'");
    expect(directive(csp, "style-src-attr")).toContain("'unsafe-inline'");
    expect(directive(csp, "frame-src")).toContain("https://api.razorpay.com");
    expect(directive(csp, "connect-src")).toContain(
      "https://api.razorpay.com",
    );
    expect(directive(csp, "connect-src")).toContain(
      "https://lumberjack.razorpay.com",
    );

    const saved = process.env.NEXT_PUBLIC_SENTRY_DSN;
    process.env.NEXT_PUBLIC_SENTRY_DSN =
      "https://public@o1.ingest.sentry.io/42";
    try {
      expect(directive(buildCsp("n"), "connect-src")).toContain(
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

  it("defaults every route to report-only", async () => {
    expect(existsSync(proxyPath)).toBe(true);
    const { proxy } = await loadProxy();

    for (const path of [...SENSITIVE.map((prefix) => `/${prefix}`), ...STATIC_ROUTES]) {
      const response = proxy(new NextRequest(`http://localhost:3000${path}`));
      const csp = policyOf(response, "Content-Security-Policy-Report-Only");
      expect(policyOf(response, "Content-Security-Policy")).toBe("");
      expect(csp).not.toBe("");
      expect(nonceOf(csp)).toBeTruthy();
      expect(directive(csp, "script-src")).not.toContain("'unsafe-inline'");
      expect(
        response.headers.get("x-middleware-request-content-security-policy"),
      ).toBe(csp);
    }
  });

  it("enforces only the interaction prefixes when the switch is flipped", async () => {
    expect(existsSync(proxyPath)).toBe(true);
    process.env[SWITCH] = "enforce";
    try {
      const { proxy } = await loadProxy();

      for (const prefix of SENSITIVE) {
        const response = proxy(
          new NextRequest(`http://localhost:3000/${prefix}`),
        );
        const csp = policyOf(response, "Content-Security-Policy");
        expect(csp).not.toBe("");
        expect(nonceOf(csp)).toBeTruthy();
        expect(
          policyOf(response, "Content-Security-Policy-Report-Only"),
        ).toBe("");
      }

      for (const path of STATIC_ROUTES) {
        const response = proxy(new NextRequest(`http://localhost:3000${path}`));
        expect(policyOf(response, "Content-Security-Policy")).toBe("");
        expect(
          policyOf(response, "Content-Security-Policy-Report-Only"),
        ).not.toBe("");
      }
    } finally {
      delete process.env[SWITCH];
    }
  });

  it("mints a fresh nonce per request and mirrors it in the policy", async () => {
    expect(existsSync(proxyPath)).toBe(true);
    const { proxy } = await loadProxy();
    const first = proxy(new NextRequest("http://localhost:3000/login"));
    const second = proxy(new NextRequest("http://localhost:3000/login"));
    const firstCsp = policyOf(first, "Content-Security-Policy-Report-Only");
    const secondCsp = policyOf(second, "Content-Security-Policy-Report-Only");
    const firstNonce = nonceOf(firstCsp);
    const secondNonce = nonceOf(secondCsp);

    expect(firstNonce).toBeTruthy();
    expect(secondNonce).toBeTruthy();
    expect(firstNonce).not.toBe(secondNonce);
    expect(directive(firstCsp, "script-src")).toContain(
      `'nonce-${firstNonce}'`,
    );
    expect(directive(firstCsp, "style-src")).toContain(
      `'nonce-${firstNonce}'`,
    );
    expect(
      first.headers.get("x-middleware-request-content-security-policy"),
    ).toBe(firstCsp);
  });

  it("leaves CSP to the proxy and keeps every other header enforced", async () => {
    const headers = await securityHeaders();
    expect(headerValue(headers, "Content-Security-Policy")).toBeUndefined();
    expect(
      headerValue(headers, "Content-Security-Policy-Report-Only"),
    ).toBeUndefined();
    expect(headerValue(headers, "X-Content-Type-Options")).toBe("nosniff");
    expect(headerValue(headers, "X-Frame-Options")).toBe("DENY");
    expect(headerValue(headers, "Referrer-Policy")).toBe(
      "strict-origin-when-cross-origin",
    );
    expect(headerValue(headers, "Permissions-Policy")).toBe(
      "camera=(), microphone=(), geolocation=()",
    );
    expect(headerValue(headers, "Cross-Origin-Opener-Policy")).toBe(
      "same-origin-allow-popups",
    );
    expect(headerValue(headers, "Cross-Origin-Resource-Policy")).toBe(
      "same-origin",
    );
    expect(headerValue(headers, "Cross-Origin-Embedder-Policy")).toBeUndefined();
    expect(nextConfig.poweredByHeader).toBe(false);
  });
});
