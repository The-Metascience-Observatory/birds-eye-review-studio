import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// The monorepo root (one level up from studio/). Pinning this stops Next from
// INFERRING a root by scanning upward for lockfiles — on a machine that happens
// to have another lockfile above the repo (a stray one in the Windows home, say)
// it picks the wrong root and prints "Detected additional lockfiles" on boot.
const workspaceRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

// If the checkout lives inside a Dropbox folder, the build cache MUST NOT be
// synced. Dropbox races Next's atomic writes — it deletes the .tmp files Next
// writes into the dist dir mid-build, so every page 500s with e.g.
//   ENOENT .next/static/development/_buildManifest.js.tmp.<rand>
//   ENOENT .next/server/pages/_app/build-manifest.json
//
// Relocating distDir out of the tree does NOT work: Turbopack refuses to write
// to a dist dir outside the project root ("Failed to write page endpoint /_app",
// Project::execution_context failed), and Next resolves distDir relative to the
// project anyway — an absolute path silently lands at studio/<abs path> INSIDE
// Dropbox, which is the very breakage being avoided.
//
// So the cache stays at the default .next and is excluded from syncing with the
// com.dropbox.ignored xattr, applied by studio/scripts/dropbox-ignore.sh via the
// npm predev/prebuild hooks. That xattr is LOST when the directory is deleted,
// so re-run the script after any `rm -rf .next` (the hooks do it for you).
// Outside Dropbox the script does nothing.

// Where the FastAPI server actually listens. SERVER-side only — this value is
// read by the Next process when it proxies /api/*, and never reaches the
// browser, which is the whole point (see the rewrites() comment below).
const apiOrigin = process.env.BER_API_ORIGIN ?? "http://127.0.0.1:8000";

// Headers applied to every response. The app shipped with none of these.
//
// The CSP is the one that matters: the session JWT lives in localStorage
// (studio/src/lib/auth.ts), so it is readable by any script that manages to run
// on the page. There is no raw-HTML sink in the app today — no
// dangerouslySetInnerHTML anywhere, and ReactMarkdown runs WITHOUT rehype-raw,
// so paper- and LLM-authored markdown is escaped — and this header is what keeps
// a future one from becoming account takeover.
//
// 'unsafe-inline' in script-src is a real weakening, and it is here because Next
// injects inline hydration bootstrap scripts. Removing it means emitting a
// per-request nonce from a middleware.ts and threading it through; worth doing,
// but it is a change to how every page renders and does not belong in the same
// pass as the auth work. Everything else is locked down: no plugins, no framing,
// no <base> hijacking, and connect-src is same-origin only — which is possible
// precisely because the API is proxied rather than called cross-origin.
const securityHeaders = [
  {
    key: "Content-Security-Policy",
    value: [
      "default-src 'self'",
      "script-src 'self' 'unsafe-inline'",
      "style-src 'self' 'unsafe-inline'",
      // data: for inlined icons; blob: for the PDF viewer's object URLs.
      "img-src 'self' data: blob:",
      "font-src 'self' data:",
      "connect-src 'self'",
      "frame-src 'self' blob:",
      "object-src 'none'",
      "base-uri 'self'",
      "form-action 'self'",
      "frame-ancestors 'none'",
    ].join("; "),
  },
  // Belt-and-braces with frame-ancestors, for anything that predates CSP.
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  // Nothing here uses these; deny them so an injected script cannot either.
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), payment=()",
  },
  // Only meaningful over HTTPS (the tunnel); ignored by browsers on plain HTTP,
  // so it is safe to send unconditionally rather than behind a mode flag.
  {
    key: "Strict-Transport-Security",
    value: "max-age=31536000; includeSubDomains",
  },
];

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // No `X-Powered-By: Next.js` — it only advertises the stack to scanners.
  poweredByHeader: false,
  // Transpile the workspace component library (ships as TS source, no build step).
  transpilePackages: ["@bird/dashboard-ui"],
  outputFileTracingRoot: workspaceRoot,

  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },

  // Proxy the API under this app's own origin.
  //
  // The browser used to call FastAPI DIRECTLY at NEXT_PUBLIC_API_BASE, a URL
  // inlined into the client bundle at build time. That is workable on one LAN
  // and falls apart the moment the app is hosted: the baked-in address is a
  // private IP no outside visitor can reach, plain HTTP inside an HTTPS page is
  // blocked as mixed content, and exposing the API on its own public hostname
  // would put it behind a second Cloudflare Access application — which answers
  // cross-origin XHR with a login redirect, breaking every request the app makes.
  //
  // Proxying instead makes the API same-origin. CORS stops being involved at
  // all, uvicorn can bind loopback so the API has no reachable socket of its
  // own, one Access application covers the whole app, and there is no
  // build-time URL left to go stale. Dev and production now take the identical
  // path, so "works locally, breaks hosted" has one less way to happen.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/:path*` }];
  },
};

export default nextConfig;
