"""Golden evaluation set runner (spec 11).

    python -m app.eval_golden            # uses ANTHROPIC_API_KEY, writes data/golden_report.{json,csv}

Builds an isolated temporary database from tests/golden/golden_leads.json, generates messages
for all 30 leads, then has a SEPARATE auditor call (which never saw the generation prompt) check
every sentence that talks about the recipient against the evidence. Reports:
  - unsupported-claim rate (target < 2%)
  - banned-phrase rate (target 0%)
  - validator pass rate, cost
and writes a CSV where you fill in your own 1-5 rating per draft.
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel
from sqlmodel import Session, SQLModel, create_engine, func, select

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden" / "golden_leads.json"

AUDITOR_SYSTEM = """You audit an outreach message for unsupported claims. You are given numbered evidence and a
message. List every sentence or clause in the message that states something about the RECIPIENT's business
(not the sender's own offer). For each, decide if the evidence directly supports it. Be strict: paraphrase is
fine, but any added fact, number, compliment or assumption not in the evidence means supported=false."""


class AuditItem(BaseModel):
    text: str
    supported: bool
    evidence_ids: list[int] = []


class AuditOut(BaseModel):
    items: list[AuditItem] = []


def build_db(session: Session, leads: list[dict]) -> list[int]:
    from app.models import Niche, PortfolioAsset
    from app.services.leads import add_contact_point, add_evidence, create_lead
    from app.services.niches import seed_niches
    seed_niches(session)
    session.flush()
    niches = {n.slug: n for n in session.exec(select(Niche))}
    for slug, n in niches.items():
        session.add(PortfolioAsset(niche_id=n.id, title=f"{n.name} reel", url=f"https://portfolio.example/{slug}",
                                   type="reel", tags="reels,captions,hooks,walkthrough,talking_head"))
    session.add(PortfolioAsset(title="Main", url="https://portfolio.example", type="main"))
    ids = []
    for i, g in enumerate(leads):
        slug = g["name"].lower().replace(" ", "").replace("&", "")[:20]
        res = create_lead(session, name=g["name"], website=f"https://{slug}.example.in", city=g["city"],
                          country=g["country"], niche_id=niches[g["niche"]].id, intent_signal=g.get("intent_signal"),
                          contact_name=g.get("contact_name"), source="golden")
        lead = res.lead
        ch = g.get("channel", "email")
        if ch == "email":
            add_contact_point(session, lead, "email", f"hello@{slug}.example.in", None)
        else:
            from app.services.normalize import profile_url
            handle = f"company/{slug}{i}" if ch == "linkedin" else f"{slug}{i}"
            add_contact_point(session, lead, ch, profile_url(ch, handle), None)
        for e in g["evidence"]:
            add_evidence(session, lead, kind=e["kind"], source_type=e["source_type"], snippet=e["snippet"],
                         source_url=f"https://{slug}.example.in")
        lead.status = "QUALIFIED"
        session.add(lead)
        ids.append(lead.id)
    session.commit()
    return ids


def run(session: Session, llm, auditor_llm=None, cfg=None) -> dict:  # noqa: ANN001
    from app.config import get_settings
    from app.models import Lead, LLMCall, Message
    from app.services.leads import evidence_of
    from app.services.messaging import Draft, GenerationBlocked, generate_message
    from app.services.validator import BANNED_PHRASES
    cfg = cfg or get_settings()
    auditor_llm = auditor_llm or llm
    leads = json.loads(GOLDEN.read_text(encoding="utf-8"))
    ids = build_db(session, leads)
    rows, total_claims, unsupported, banned_drafts, drafts, valid = [], 0, 0, 0, 0, 0
    for lead_id, g in zip(ids, leads):
        lead = session.get(Lead, lead_id)
        try:
            msg = generate_message(session, lead, g.get("channel", "email"), llm=llm, cfg=cfg)
            session.commit()
        except GenerationBlocked as e:
            rows.append({"lead": g["name"], "error": str(e)})
            continue
        v = json.loads(msg.validation_json)
        valid += msg.status == "APPROVAL_PENDING"
        ev = "\n".join(f"[{e.id}] {e.snippet}" for e in evidence_of(session, lead_id) if e.kind != "opportunity")
        for label in ("a", "b"):
            d = Draft.model_validate_json(msg.draft_a if label == "a" else msg.draft_b)
            drafts += 1
            low = d.body.lower()
            if any(p in low for p in BANNED_PHRASES):
                banned_drafts += 1
            audit = auditor_llm.json(task="golden_audit", model=cfg.model_judge, system=AUDITOR_SYSTEM,
                                     content=f"Evidence:\n{ev}\n\nMessage:\n{d.body}", schema=AuditOut, max_tokens=800)
            total_claims += len(audit.items)
            bad = [a.text for a in audit.items if not a.supported]
            unsupported += len(bad)
            rows.append({"lead": g["name"], "channel": msg.channel, "draft": label,
                         "selected": v["selected"] == label, "valid": v[label]["ok"], "subject": d.subject or "",
                         "body": d.body, "unsupported": " | ".join(bad), "your_rating_1_to_5": ""})
    cost = float(session.exec(select(func.coalesce(func.sum(LLMCall.est_cost_inr), 0.0))).one())
    n_msgs = len(session.exec(select(Message)).all())
    return {
        "leads": len(leads), "messages": n_msgs, "drafts": drafts,
        "unsupported_claim_rate": round(unsupported / total_claims, 4) if total_claims else 0.0,
        "banned_phrase_rate": round(banned_drafts / drafts, 4) if drafts else 0.0,
        "validator_pass_rate": round(valid / n_msgs, 4) if n_msgs else 0.0,
        "targets_met": (unsupported / total_claims if total_claims else 0) < 0.02 and banned_drafts == 0,
        "est_cost_inr": round(cost, 2), "rows": rows,
    }


def main() -> int:
    from app.llm import LLM
    import app.models  # noqa: F401
    tmp = tempfile.mkdtemp()
    engine = create_engine(f"sqlite:///{tmp}/golden.db")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        report = run(s, LLM(s))
    out = ROOT / "data"
    out.mkdir(exist_ok=True)
    (out / "golden_report.json").write_text(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))
    with open(out / "golden_report.csv", "w", newline="", encoding="utf-8") as f:
        rows = [r for r in report["rows"] if "body" in r]
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))
    print(f"\nRate each draft in {out / 'golden_report.csv'} (column your_rating_1_to_5).")
    return 0 if report["targets_met"] else 1


if __name__ == "__main__":
    sys.exit(main())
