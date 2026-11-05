# Security

Please report suspected vulnerabilities privately, not in a public issue: use
GitHub's private vulnerability reporting (the repository's **Security** tab →
**Report a vulnerability**). Include steps to reproduce and the affected
component (pipeline scripts, studio API, or studio frontend).

When self-hosting the studio, read [docs/hosting.md](docs/hosting.md) first: run with
`PUBLIC_MODE=true`, set strong `JWT_SECRET` / `APP_SECRET` values, keep the API
and Redis on loopback, and keep `server/.env`, `.env.local`, the database and
`server/_data/` out of any synced or shared folder.
