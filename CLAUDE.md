# CLAUDE.md — Proof-First Client Outreach System

Full spec: `BUILD_SPEC.md`. These rules override any feature request.

## 1. Non-negotiable rules

1. **No scraping of Instagram, LinkedIn, Facebook or X.** No logged-in automation, no cookies or session tokens, no fake accounts, no CAPTCHA or rate-limit bypass, no browser-fingerprint tricks, no unofficial "API" vendors.
2. **Social analysis is human-fed.** The user supplies a public profile URL plus pasted bio/captions and screenshots of public posts. Claude analyses what was supplied.
3. **Automated sending exists for email only**, through official Gmail (or Microsoft) OAuth from the user's own mailbox. Instagram, LinkedIn, Facebook, X, WhatsApp are **handoff channels**: the app prepares the message and portfolio link, the human pastes and sends.
4. **Every personalised claim must cite an evidence row.** No evidence, no claim. No invented compliments, no fake familiarity.
5. **Compliance logic is plain code, never an LLM:** suppression check, daily cap, per-organisation contact lock (90 days), unsubscribe handling, send window.
6. **Approval required** for every initial message and every follow-up (default, configurable only for replies in an existing thread).
7. **Default `SEND_ENABLED=false`** (dry run). Real sending needs explicit config.
8. **Never store passwords, cookies or raw tokens in plaintext.** OAuth tokens encrypted at rest (Fernet key from env). Secrets only in `.env` (git-ignored).
9. **Any opt-out, "not interested", "stop", "unsubscribe" in any language adds the contact and its organisation to suppression immediately** (keyword rule first, LLM second).
10. Do not contact EU/UK-based recipients in the MVP unless `ALLOW_EU_UK=true`.
11. Medical niche: never make or suggest health, outcome or before/after claims in outreach copy; only talk about editing craft.

If a feature would require scraping or session automation, or conflicts with a platform's terms, do not implement it. Propose the compliant handoff alternative.

## 2. Channel capability matrix

| Channel | Discover | Analyse | Send | Implementation |
|---|---|---|---|---|
| Google Places API | Yes | n/a | n/a | Official API, billing enabled, key restricted; persist `place_id` only |
| Business websites | Yes | Yes | n/a | Polite fetch, robots.txt, identifiable user agent |
| Email | n/a | n/a | **Yes, after approval** | Gmail API OAuth, caps, unsubscribe |
| Instagram | No | Human-fed (URL, text, screenshots) | **Handoff only** | Copy message + open profile link + mark sent |
| LinkedIn | No | Human-fed | **Handoff only** | Same |
| Facebook Pages | No | Human-fed | **Handoff only** | Same |
| X | No | Human-fed | **Handoff only** | Same |
| YouTube | Optional Data API (metadata) | Public metadata, user-supplied URL | No | Phase 6 |
| Instagram/Messenger inbound | n/a | n/a | Reply only to people who messaged first (24h window) | Phase 6, needs Meta app review |

## 3. Model routing

| Task | Env var | Default | Notes |
|---|---|---|---|
| Screenshot/caption analysis, qualification judgement, final message | `MODEL_JUDGE` | `claude-opus-5-5` | Judgement and copy quality; low volume |
| Bulk website text extraction, tagging | `MODEL_EXTRACT` | `claude-sonnet-5-5` | Cheaper structured extraction |
| Reply classification, language detection | `MODEL_CLASSIFY` | `claude-haiku-5-5` | Keyword rules override |

All LLM calls go through `app/llm.py` (retries, timeouts, Pydantic validation, cost logging to `llm_calls`, hard stop at `MONTHLY_LLM_BUDGET_INR`).

## 4. Testing rules

- Unit tests: URL/email/phone/handle normalisation, dedup, organisation lock, suppression (including hashed lookup), daily caps, send-window logic, keyword opt-out detection (English + Hinglish), claim validator, portfolio matcher.
- Integration tests with mocked Places, Gmail and Anthropic clients. Tests never hit the network.
- Golden evaluation set (`tests/golden/`): unsupported-claim rate < 2%, banned-phrase rate 0%.
- Dry-run end-to-end test must pass before `SEND_ENABLED=true`.
- Deliverability before first real send: SPF, DKIM, DMARC; test to own addresses; warm up 5–10/day.

## 5. Working conventions

- Run tests: `.venv/bin/pytest -q`
- Migrations: `alembic revision --autogenerate -m "..."` then `alembic upgrade head`
- Run app: `.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000` (exactly one worker; jobs run in-process)
