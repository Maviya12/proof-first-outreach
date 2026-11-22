# Proof First Outreach deployment

## Architecture review

The repository contains the shared build spec and Git commits labeled Claude for Phases 0–6. The implemented architecture is a single-owner FastAPI application with Jinja/HTMX, SQLite in WAL mode, an in-process APScheduler, and Anthropic. The Sites deployment adds a private ChatGPT-authenticated gateway while retaining the Python service, its login, and one scheduler process. No live Claude consultation took place during this review.

The original SaaS brief also called for multi-user workspaces and Instagram and LinkedIn prospect discovery. This release is scoped to a single operator. Social intake starts from user-provided profile information and outreach uses a manual handoff. Gmail uses OAuth when configured. Meta inbound, real-provider AI evaluation, scheduled operation in production, and production restore still need verification. The EU/UK outreach gate defaults to off.

## Free hosting

Railway Free provides one 0.5 GB volume, a provider HTTPS hostname and $1 in monthly usage. The service sleeps when idle and stops on allowance exhaustion. Follow-up jobs and reply polling depend on the process being awake. Keep the deployment in a Free workspace. [Railway Free plan](https://docs.railway.com/pricing/free-plan)

Use one Docker service and mount its persistent volume at `/data`. Configure `DATABASE_URL=sqlite:////data/outreach.db`, `UPLOAD_DIR=/data/uploads`, `SEND_ENABLED=false` and `META_INBOUND_ENABLED=false`. Keep the HTTP health check at `/livez`. The entrypoint migrates and seeds the database before starting Uvicorn with one worker.

Set `ADMIN_PASSWORD_HASH`, `SESSION_SECRET`, `FERNET_KEY` and `SITES_ORIGIN_TOKEN` in the provider's secret store. Use `python -m app.cli gen-keys` and `python -m app.cli hash-password` for application credentials and a separate cryptographically random origin token. Add the Anthropic and Google configuration only through authorized provider access. Configure `GOOGLE_OAUTH_REDIRECT_URI` to the Sites HTTPS origin plus `/api/v1/integrations/google/callback` if Gmail is enabled.

In the sibling Sites checkout, configure `PROOF_FIRST_ORIGIN` to the backend HTTPS origin and `PROOF_FIRST_ORIGIN_TOKEN` to the same value as the backend's `SITES_ORIGIN_TOKEN`. Keep Site access owner-only. `node build.mjs` produces the Worker deployment output. The gateway forwards the application session only and denies cross-origin writes. The public backend health route returns an empty response and the application routes require the shared token.

## Release and maintenance

The local release checks passed 176 Python tests, including the dry-run pipeline and migration parity, and two gateway tests. The Worker build passed the JavaScript syntax check. Docker image execution and hosted integration tests require the Python hosting environment.

Before sharing the production URL, exercise owner sign-in, CSV import, profile intake, evidence review, qualification, draft approval, portfolio matching, manual handoff, reply logging and the dashboard using test records. Retest persisted records after a restart. Verify the isolated origin denies direct access and test the authorized model provider separately. Leave outbound delivery disabled until an authorized end-to-end delivery check is complete.

Monitor host logs, disk usage, scheduler progress and the Railway allowance. Use SQLite's online backup API or SQLite CLI `.backup` for the live database, and snapshot uploads alongside it. Store encrypted copies outside the app volume and verify restoring them into an isolated environment. Keep the Fernet key in a separate secret backup for OAuth recovery. Rotate the gateway token on both services together.
