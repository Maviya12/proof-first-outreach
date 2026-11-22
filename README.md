# Proof-First Client Outreach (video editor)

A single-user tool that finds prospects, researches them from public information, writes one evidence-grounded message per prospect, and sends email (after your approval) or prepares DMs for you to send yourself. The full spec is in `BUILD_SPEC.md` and the non-negotiable rules are in `CLAUDE.md`.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m app.cli gen-keys          # paste FERNET_KEY and SESSION_SECRET into .env
python -m app.cli hash-password     # paste ADMIN_PASSWORD_HASH into .env
# also set ANTHROPIC_API_KEY, SENDER_NAME, SENDER_EMAIL, SENDER_CONTACT_ADDRESS
python -m app.cli init-db           # runs migrations + seeds niches
uvicorn app.main:app --host 127.0.0.1 --port 8000    # exactly one worker
```

Open http://127.0.0.1:8000 and log in. The app binds to localhost. Don't expose it until it sits behind HTTPS.

## Daily workflow

1. **Discovery**: import a CSV of leads you found by hand (Meta Ad Library, job posts, directories). Google Places search is off until you set `PLACES_DISCOVERY_ENABLED=true`; see "Before you turn things on".
2. **Lead page → Enrich website**: the app fetches up to 8 public pages (it respects robots.txt and waits 2 s between requests). Contacts come only from the business's own site. Every website snippet is checked word-for-word against the page before it is stored as evidence.
3. **Lead page → Public Profile Intake**: paste the bio and captions and upload up to 8 screenshots. Opus returns observations you can check, each one citing `screenshot:N`, `caption:N` or `bio`. Unsafe or uncited observations are dropped in code.
4. **Qualify**: a rubric score plus Opus's reason. Leads below the threshold become `NOT_A_FIT`.
5. **Portfolio**: add 3–5 assets per niche and one `main` asset.
6. **Generate drafts**: Opus writes two drafts → a code validator checks them → Opus picks one. Each claim cites evidence IDs.
7. **Approvals**: review the evidence chips, compare A and B, edit, then Approve or Reject.
   - Email: sent through Gmail only if `SEND_ENABLED=true`; otherwise it's a dry run. Caps or the send window may schedule it for later.
   - DMs: go to the **Handoff** queue → Copy → Open profile → paste and send yourself → Mark as sent.
8. **Inbox**: Gmail replies sync every 5 minutes. Paste replies from other channels on the lead page. Opt-outs are suppressed at once.
9. **Follow-ups**: at most 2 (default 4 and 9 days after the previous message). They are drafted when due and go through approval. Any reply stops them.

## Phases (all implemented)

| Phase | What | Key files |
|---|---|---|
| 0 | Config, schema + Alembic, auth (argon2 + session), audit log, hashed suppression, dedup, caps/window/org-lock, LLM wrapper with budget stop | `app/config.py`, `app/models.py`, `app/services/{suppression,dedup,compliance,normalize}.py`, `app/llm.py` |
| 1 | CSV import, Places discovery (stores `place_id` only), polite enrichment, qualification | `app/services/{csv_import,places,enrichment,qualification}.py` |
| 2 | Public Profile Intake + vision analysis | `app/services/intake.py`, `app/prompts/analysis_system.md` |
| 3 | Portfolio matcher, two drafts + validator + judge, golden eval set | `app/services/{portfolio,validator,messaging}.py`, `app/eval_golden.py` |
| 4 | Gmail OAuth, fail-closed dispatcher, handoff, reply sync + classifier, UI | `app/services/{gmail,dispatcher,replies,reply_sync}.py`, `app/ui/` |
| 5 | Follow-ups, pricing/package reply drafts, funnel | `app/services/{followups,analytics}.py` |
| 6 | Meta inbound webhook (24 h window), YouTube metadata, 3 extra niches | `app/services/{meta_inbound,youtube}.py`, `app/api/routes_meta.py` |

## UI assets

Tailwind CSS and htmx are served locally from `app/ui/static/` (no CDN at runtime). After changing templates, rebuild the CSS:

```bash
npx tailwindcss@3 -i app/ui/static/tailwind.src.css -o app/ui/static/app.css --content 'app/ui/templates/**/*.html' --minify
```

## Tests

```bash
pytest -q                         # all tests use mocked network, Anthropic, Gmail and Meta
python -m app.cli dry-run-check   # end-to-end dry run; must pass before SEND_ENABLED=true
python -m app.eval_golden         # REAL API: 30-lead golden set → unsupported-claim and banned-phrase rates
```

`eval_golden` writes `data/golden_report.csv`. Rate each draft there (1–5) to measure the Phase 3 "70% approved with light edits" target.

## Before you turn things on

- **Places**: read the current Google Maps Platform Service Specific Terms (Places caching table). Confirm that building a prospect list is allowed before setting `PLACES_DISCOVERY_ENABLED=true`. If it isn't, use CSV import.
- **Gmail**: create a Google Cloud OAuth client (Web), add the redirect URI from `.env`, and enable the Gmail API. Scopes are `gmail.send` and `gmail.readonly`. In "Testing" status the refresh token expires after 7 days, so reconnect weekly, use a Workspace "Internal" app, or complete verification.
- **Deliverability**: set up SPF, DKIM and DMARC on the sending domain, send test emails to your own addresses, then warm up at 5–10 per day.
- **Sender identity**: `SENDER_NAME` and `SENDER_CONTACT_ADDRESS` go into every email footer, along with the opt-out line (CAN-SPAM / DPDP notice). Sending refuses to run if they are missing.
- **Meta inbound (Phase 6)**: needs Meta app review, a public HTTPS URL for `/webhooks/meta` (for example a tunnel), and `META_*` values in `.env`. It only replies to people who messaged first, inside 24 hours. Outside that window the reply goes to the handoff queue.
- **Privacy**: screenshots are sent to the Anthropic API for analysis, so treat Anthropic as a data processor in your privacy notes. Raw HTML and screenshots are deleted after `RAW_RETENTION_DAYS`. "Erase" on a lead page deletes everything except the suppression hashes. Have an Indian privacy lawyer review the email notice text.

This is not legal advice.
