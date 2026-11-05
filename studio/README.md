# Studio (Bird's Eye Review)

Next.js 15 (App Router, TypeScript, React Query, Tailwind) frontend for the
Bird's Eye Review studio. It drives the FastAPI backend in
[`../server/`](../server/) and renders dashboards with the shared
`@bird/dashboard-ui` workspace package (`../packages/dashboard-ui`).

## Run it

From the **repo root**:

```bash
bash dev.sh up        # API, both job workers and the studio (needs redis)
bash dev.sh status
bash dev.sh down
```

The studio is at <http://localhost:3002>, the API at `:8000`. `dev.sh` finds
node + pnpm and installs workspace dependencies on first run.

The browser only ever talks to the studio's own origin: API calls go to the
same-origin path `/api`, which `next.config.mjs` proxies to the FastAPI server
(`BER_API_ORIGIN`, default `http://127.0.0.1:8000`). No environment file is
needed; see `.env.local.example` for the two optional overrides.

## Layout

- `src/app/` — routes (`/reviews/[id]/stages/[slug]` is the wizard)
- `src/lib/api/` — typed API client, one module per domain, all through
  `client.ts`; import from `@/lib/api`
- `src/lib/types.ts` — hand-maintained mirror of `server/app/schemas.py`
  (checked by `server/tests/test_contract_drift.py`; update both together)
- `src/components/` — stage bodies, the evidence table/dock, the paper drawer

## Checks

```bash
cd studio
npx tsc --noEmit
rm -rf .next && npx next build    # a stale .next gives false ENOENT errors
```
