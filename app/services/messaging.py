"""Message generation (spec 6.4): two Opus drafts -> deterministic validator -> Opus judge.

Generation is refused (plain code) when the lead is suppressed, blocked, EU/UK-gated, lacks a
recipient on that channel, has insufficient intake, or has no citable evidence.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field
from rapidfuzz import fuzz
from sqlmodel import Session, select

from app.audit import audit
from app.config import Settings, get_settings
from app.llm import LLM, load_prompt
from app.models import (EMAIL_CHANNEL, HANDOFF_CHANNELS, Evidence, Lead, LeadStatus, Message, MessageEvidence,
                        MessageStatus, Niche, Organization, PortfolioAsset)
from app.services.compliance import is_eu_uk
from app.services.leads import contacts_of, evidence_of, set_status
from app.services.niches import niche_config
from app.services.portfolio import select_asset
from app.services.settings_store import all_settings
from app.services.suppression import address_suppressed, lead_suppression_reasons
from app.services.validator import CITABLE_KINDS_EXCLUDED, Claim, ValidationResult, validate_draft


class GenerationBlocked(Exception):
    pass


class DraftClaim(BaseModel):
    text: str
    evidence_ids: list[int] = []


class Draft(BaseModel):
    subject: str | None = None
    body: str
    claims: list[DraftClaim] = []


class DraftPair(BaseModel):
    draft_a: Draft
    draft_b: Draft


class JudgeOut(BaseModel):
    score_a: int = Field(ge=1, le=10)
    score_b: int = Field(ge=1, le=10)
    selected: Literal["a", "b"]
    reason: str


def recipient_for(session: Session, lead: Lead, channel: str) -> str | None:
    types = ["phone", "whatsapp"] if channel == "whatsapp" else [channel]
    for t in types:
        for cp in contacts_of(session, lead.id, t):
            if cp.is_public_business_contact and not address_suppressed(session, channel if t != "phone" else "phone", cp.value_norm):
                return cp.value_norm
    return None


def check_can_generate(session: Session, lead: Lead, channel: str, kind: str, cfg: Settings) -> str:
    """Return the recipient address or raise GenerationBlocked with the reasons."""
    reasons = lead_suppression_reasons(session, lead)
    if channel not in HANDOFF_CHANNELS | {EMAIL_CHANNEL}:
        reasons.append(f"unknown channel {channel}")
    allowed_states = {"initial": {LeadStatus.QUALIFIED, LeadStatus.MESSAGE_READY, LeadStatus.APPROVAL_PENDING},
                      "followup": {LeadStatus.SENT}, "reply": None}[kind]
    if allowed_states and lead.status not in allowed_states:
        reasons.append(f"lead status {lead.status} (needs {' or '.join(sorted(allowed_states))})")
    if lead.intake_status == "INSUFFICIENT":
        reasons.append("profile intake insufficient; supply more material first")
    org = session.get(Organization, lead.organization_id)
    to = recipient_for(session, lead, channel)
    if not to:
        reasons.append(f"no usable {channel} contact")
    if not cfg.allow_eu_uk and org and is_eu_uk(org.country, to if channel == EMAIL_CHANNEL else org.domain_norm):
        reasons.append("EU/UK recipient (ALLOW_EU_UK=false)")
    citable = [e for e in evidence_of(session, lead.id) if e.kind not in CITABLE_KINDS_EXCLUDED]
    if kind != "reply" and not citable:
        reasons.append("no evidence to ground a message")
    if reasons:
        raise GenerationBlocked("; ".join(sorted(set(reasons))))
    return to


def evidence_map(session: Session, lead_id: int) -> dict[int, Evidence]:
    return {e.id: e for e in evidence_of(session, lead_id)}


def validate_for_lead(session: Session, lead: Lead, *, body: str, subject: str | None, channel: str,
                      claims: list[Claim], asset: PortfolioAsset | None, kind: str = "initial",
                      human_edit: bool = False) -> ValidationResult:
    niche = session.get(Niche, lead.niche_id) if lead.niche_id else None
    ncfg = niche_config(niche)
    st = all_settings(session)
    extra = [st["booking_link"]] if st.get("booking_link") else []
    return validate_draft(body=body, subject=subject, channel=channel, claims=claims,
                          evidence=evidence_map(session, lead.id), portfolio_url=asset.url if asset else None,
                          is_medical=bool(ncfg.get("is_medical")), niche_banned=ncfg.get("banned_claims"),
                          sample_offer_enabled=bool(st.get("sample_offer_enabled")), kind=kind,
                          human_edit=human_edit, extra_allowed_urls=extra)


def _claims(d: Draft) -> list[Claim]:
    return [Claim(c.text, c.evidence_ids) for c in d.claims]


def _context(session: Session, lead: Lead, channel: str, asset: PortfolioAsset, kind: str, followup_number: int,
             previous: list[Message]) -> str:
    org = session.get(Organization, lead.organization_id)
    niche = session.get(Niche, lead.niche_id) if lead.niche_id else None
    ncfg = niche_config(niche)
    st = all_settings(session)
    evidence = evidence_of(session, lead.id)
    used = set()
    for m in previous:
        used |= set(session.exec(select(MessageEvidence.evidence_id).where(MessageEvidence.message_id == m.id)))
    ev_lines = []
    for e in evidence:
        if e.kind in CITABLE_KINDS_EXCLUDED:
            continue
        tag = " (ALREADY USED in an earlier message — prefer a different one)" if e.id in used else ""
        ev_lines.append(f"[{e.id}] ({e.kind}, source: {e.source_type} {e.source_url or e.source_ref or ''}){tag}: {e.snippet}")
    ideas = [f"- {e.snippet}" for e in evidence if e.kind == "opportunity"]
    is_dm = channel != EMAIL_CHANNEL
    lines = [
        f"Channel: {channel} ({'DM: 40-80 words, no subject' if is_dm else 'email: 70-120 words + subject'})",
        f"Message kind: {kind}" + (f" (follow-up #{followup_number}: must add something NEW — a different observation "
                                    f"or a short sample idea; never repeat earlier text)" if kind == 'followup' else ""),
        f"Business: {org.name} — {org.city or ''} {org.country or ''}",
        f"Recipient: {lead.contact_name or 'unknown name (use a neutral greeting like Hi there / Hi <business> team)'}"
        + (f", {lead.contact_role}" if lead.contact_role else ""),
        f"Niche: {niche.name if niche else 'general'} | medical niche: {bool(ncfg.get('is_medical'))}",
        f"Playbook: {ncfg.get('playbook', '')}",
        f"Tone: {st['tone']} | Language: {st['language']} | sample_offer_enabled: {st['sample_offer_enabled']}",
        f"Portfolio asset to include exactly: {asset.title} — {asset.url}",
        "",
        "Evidence (cite by the integer id in brackets):",
        *ev_lines,
    ]
    if ideas:
        lines += ["", "Editing ideas from analysis (may inspire the offer; NOT citable facts):", *ideas]
    if previous:
        lines += ["", "Earlier messages to this lead (do not repeat):"] + [f"---\n{m.body}" for m in previous if m.body]
    return "\n".join(lines)


def generate_message(session: Session, lead: Lead, channel: str, *, llm: LLM, kind: str = "initial",
                     followup_number: int = 0, cfg: Settings | None = None, actor: str = "system") -> Message:
    cfg = cfg or get_settings()
    to = check_can_generate(session, lead, channel, kind, cfg)
    asset = select_asset(session, lead)
    if asset is None:
        raise GenerationBlocked("no eligible portfolio asset (add a niche asset or a 'main' asset)")

    previous = list(session.exec(select(Message).where(Message.lead_id == lead.id,
                                                       Message.status.in_([MessageStatus.SENT, MessageStatus.SENT_MANUAL]))
                                 .order_by(Message.id)))
    context = _context(session, lead, channel, asset, kind, followup_number, previous)
    system = load_prompt("message_system.md")

    def _run(extra: str = "") -> tuple[DraftPair, ValidationResult, ValidationResult]:
        pair = llm.json(task=f"message_{kind}", model=cfg.model_judge, system=system, content=context + extra,
                        schema=DraftPair, max_tokens=2000)
        va = validate_for_lead(session, lead, body=pair.draft_a.body, subject=pair.draft_a.subject, channel=channel,
                               claims=_claims(pair.draft_a), asset=asset, kind=kind)
        vb = validate_for_lead(session, lead, body=pair.draft_b.body, subject=pair.draft_b.subject, channel=channel,
                               claims=_claims(pair.draft_b), asset=asset, kind=kind)
        if kind == "followup":
            for d, v in ((pair.draft_a, va), (pair.draft_b, vb)):
                for p in previous:
                    if p.body and fuzz.ratio(d.body.lower(), p.body.lower()) > 60:
                        v.error("follow-up repeats an earlier message; it must add something new")
                        break
        return pair, va, vb

    pair, va, vb = _run()
    attempts = 1
    if not va.ok and not vb.ok:
        feedback = (f"\n\nYour previous drafts failed validation. Fix every issue:\nDraft A: {va.errors}\n"
                    f"Draft B: {vb.errors}\n")
        pair, va, vb = _run(feedback)
        attempts = 2

    judge = None
    if va.ok and vb.ok:
        jtext = (f"Evidence:\n{context.split('Evidence (cite by the integer id in brackets):', 1)[-1]}\n\n"
                 f"Draft A:\n{pair.draft_a.model_dump_json()}\n\nDraft B:\n{pair.draft_b.model_dump_json()}")
        judge = llm.json(task="message_judge", model=cfg.model_judge, system=load_prompt("judge_system.md"),
                         content=jtext, schema=JudgeOut, max_tokens=400)
        sel = judge.selected
    elif va.ok:
        sel = "a"
    elif vb.ok:
        sel = "b"
    else:
        sel = "a"
    chosen = pair.draft_a if sel == "a" else pair.draft_b
    chosen_v = va if sel == "a" else vb

    msg = Message(lead_id=lead.id, channel=channel, kind=kind, followup_number=followup_number,
                  draft_a=pair.draft_a.model_dump_json(), draft_b=pair.draft_b.model_dump_json(),
                  body=chosen.body.strip(), subject=(chosen.subject or None) if channel == EMAIL_CHANNEL else None,
                  to_address=to, portfolio_asset_id=asset.id,
                  status=MessageStatus.APPROVAL_PENDING if chosen_v.ok else MessageStatus.DRAFT,
                  validation_json=json.dumps({"a": va.as_dict(), "b": vb.as_dict(), "selected": sel, "attempts": attempts,
                                              "judge": judge.model_dump() if judge else None}))
    session.add(msg)
    session.flush()
    for eid in sorted({i for c in chosen.claims for i in c.evidence_ids}):
        if session.get(Evidence, eid) and session.get(Evidence, eid).lead_id == lead.id:
            session.add(MessageEvidence(message_id=msg.id, evidence_id=eid))
    if kind == "initial" and chosen_v.ok:
        set_status(session, lead, LeadStatus.MESSAGE_READY, actor=actor)
        set_status(session, lead, LeadStatus.APPROVAL_PENDING, actor=actor)
    audit(session, "message.generate", actor=actor, entity="message", entity_id=msg.id, channel=channel, kind=kind,
          valid=chosen_v.ok)
    return msg


def apply_edit(session: Session, msg: Message, *, body: str | None = None, subject: str | None = None,
               select: str | None = None, actor: str = "admin") -> ValidationResult:
    """Human edit or A/B switch. Re-validates; returns result. On success -> APPROVAL_PENDING."""
    if msg.status not in (MessageStatus.DRAFT, MessageStatus.APPROVAL_PENDING):
        raise ValueError(f"cannot edit a message in status {msg.status}")
    lead = session.get(Lead, msg.lead_id)
    asset = session.get(PortfolioAsset, msg.portfolio_asset_id) if msg.portfolio_asset_id else None
    v = json.loads(msg.validation_json or "{}")
    human = False
    if select in ("a", "b"):
        d = Draft.model_validate_json(msg.draft_a if select == "a" else msg.draft_b)
        msg.body, msg.subject = d.body, d.subject if msg.channel == EMAIL_CHANNEL else None
        v["selected"] = select
    if body is not None:
        msg.body, human = body.strip(), True
    if subject is not None:
        msg.subject = subject.strip() or None
    sel = v.get("selected", "a")
    src = msg.draft_a if sel == "a" else msg.draft_b
    claims = _claims(Draft.model_validate_json(src)) if src else []
    res = validate_for_lead(session, lead, body=msg.body or "", subject=msg.subject, channel=msg.channel,
                            claims=claims, asset=asset, kind=msg.kind, human_edit=human or bool(v.get("human_edit")))
    v["final"] = res.as_dict()
    v["human_edit"] = human or bool(v.get("human_edit"))
    msg.validation_json = json.dumps(v)
    msg.status = MessageStatus.APPROVAL_PENDING if res.ok else MessageStatus.DRAFT
    session.add(msg)
    if res.ok and msg.kind == "initial" and lead.status in (LeadStatus.QUALIFIED, LeadStatus.MESSAGE_READY):
        set_status(session, lead, LeadStatus.MESSAGE_READY, actor=actor)
        set_status(session, lead, LeadStatus.APPROVAL_PENDING, actor=actor)
    audit(session, "message.edit", actor=actor, entity="message", entity_id=msg.id, valid=res.ok, select=select)
    return res
