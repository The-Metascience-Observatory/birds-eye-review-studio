/** Typed HTTP client for the FastAPI server, split by domain. Import from
 *  `@/lib/api` — this barrel re-exports every module, so callers never need
 *  to know which file a function lives in.
 *
 *  Every request goes through `client.ts` (`request` for JSON, `authFetch` for
 *  multipart / blob / streaming), which owns the base URL, the Bearer header,
 *  error-detail parsing and the expired-session redirect. */

export { API_BASE, ApiError } from "./client";
export * from "./account";
export * from "./reviews";
export * from "./jobs";
export * from "./evidence";
export * from "./dashboard";
export * from "./agent";
export * from "./report";
export * from "./publish";
export * from "./admin";
