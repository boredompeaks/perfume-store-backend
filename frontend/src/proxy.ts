// task: T-20261119-006
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { buildCsp, cspHeaderName, enforceFor } from "./lib/csp";

function nonce(): string {
  return btoa(crypto.randomUUID());
}

export function proxy(request: NextRequest) {
  const requestNonce = nonce();
  const csp = buildCsp(requestNonce);
  const mode = enforceFor(request.nextUrl.pathname);

  // Next reads the nonce from the request's CSP header and stamps it on the
  // inline bootstrap/RSC scripts it renders. The response header is what the
  // browser applies; it is report-only until the switch selects a route.
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("Content-Security-Policy", csp);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set(cspHeaderName(mode), csp);
  return response;
}

// Documents only: static chunks, the image optimizer, API responses, favicon
// and files with an extension carry no HTML to nonce. Prefetch responses are
// RSC payloads, not documents, so they skip the per-request policy too.
export const config = {
  matcher: [
    {
      source: "/((?!api|_next/static|_next/image|favicon.ico|.*\\.[^/]+$).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
