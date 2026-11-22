"""Polite website enrichment (spec 6.2).

- identifiable User-Agent, robots.txt respected, 1 request / 2 s per domain, max 8 pages
- deterministic extraction first (emails on public pages, tel: links, social links, schema.org)
- MODEL_EXTRACT summarises; every quoted snippet is verified against the fetched text before it
  becomes evidence, so the model cannot invent website facts
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from rapidfuzz import fuzz
from sqlmodel import Session

from app.config import Settings, get_settings
from app.llm import LLM, load_prompt
from app.models import Lead, LeadStatus, Organization, RawFile, utcnow
from app.services.compliance import is_eu_uk, to_country_code
from app.services.leads import add_contact_point, add_evidence, set_status
from app.services.normalize import normalize_domain, normalize_handle

MAX_PAGES = 8
PAGE_HINTS = ["about", "contact", "team", "our-team", "doctors", "staff", "services", "treatments", "story",
              "who-we-are", "meet", "agents", "portfolio", "work", "videos", "gallery"]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
JUNK_EMAIL = re.compile(r"\.(png|jpe?g|gif|webp|svg)$|@(example\.|sentry|wixpress|domain\.com)", re.I)
SOCIAL_HOST_MAP = {"instagram.com": "instagram", "facebook.com": "facebook", "fb.com": "facebook",
                   "linkedin.com": "linkedin", "twitter.com": "x", "x.com": "x", "youtube.com": "youtube"}
VIDEO_HOSTS = ("youtube.com/embed", "youtube-nocookie.com", "player.vimeo.com", "youtu.be", "wistia", "vidyard")


@dataclass
class PageData:
    url: str
    title: str = ""
    site_name: str = ""
    text: str = ""
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    socials: dict[str, set[str]] = field(default_factory=dict)
    jsonld: list[dict] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    video_embeds: int = 0


class PoliteFetcher:
    def __init__(self, client: httpx.Client | None = None, cfg: Settings | None = None,
                 delay: float = 2.0, sleep=time.sleep, clock=time.monotonic):
        self.cfg = cfg or get_settings()
        self.client = client or httpx.Client(timeout=15.0, follow_redirects=True,
                                             headers={"User-Agent": self.cfg.fetch_user_agent})
        self.delay, self.sleep, self.clock = delay, sleep, clock
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last: dict[str, float] = {}
        self.requests = 0

    def _throttle(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            wait = self.delay - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        self._last[host] = self.clock()

    def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        key = f"{parts.scheme}://{parts.netloc}"
        if key not in self._robots:
            rp = RobotFileParser()
            self._throttle(parts.netloc)
            try:
                r = self.client.get(f"{key}/robots.txt", headers={"User-Agent": self.cfg.fetch_user_agent})
                self.requests += 1
                if r.status_code in (401, 403):
                    rp.parse(["User-agent: *", "Disallow: /"])
                elif r.status_code >= 400:
                    rp.parse([])  # no robots.txt: allowed
                else:
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError:
                rp.parse(["User-agent: *", "Disallow: /"])  # can't read robots: fail closed
            self._robots[key] = rp
        return self._robots[key]

    def allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        return rp.can_fetch(self.cfg.fetch_user_agent, url) if rp else False

    def get(self, url: str) -> str | None:
        if not self.allowed(url):
            return None
        self._throttle(urlsplit(url).netloc)
        try:
            r = self.client.get(url, headers={"User-Agent": self.cfg.fetch_user_agent})
            self.requests += 1
        except httpx.HTTPError:
            return None
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or "html" not in ctype:
            return None
        return r.text[:2_000_000]


def parse_page(url: str, html: str) -> PageData:
    soup = BeautifulSoup(html, "html.parser")
    pd = PageData(url=url)
    pd.title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    og = soup.find("meta", attrs={"property": "og:site_name"})
    pd.site_name = (og.get("content") or "").strip() if og else ""
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        pd.jsonld.extend(i for i in items if isinstance(i, dict))
    pd.video_embeds = len(soup.find_all("video")) + sum(
        1 for f in soup.find_all("iframe") if any(h in (f.get("src") or "") for h in VIDEO_HOSTS))

    base_host = normalize_domain(url)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("mailto:"):
            e = href[7:].split("?")[0].strip()
            if EMAIL_RE.fullmatch(e) and not JUNK_EMAIL.search(e):
                pd.emails.add(e.lower())
            continue
        if href.startswith("tel:"):
            pd.phones.add(href[4:].strip())
            continue
        absu = urljoin(url, href)
        host = normalize_domain(absu)
        for h, platform in SOCIAL_HOST_MAP.items():
            if host == h or host.endswith("." + h):
                pd.socials.setdefault(platform, set()).add(absu)
                break
        else:
            if host == base_host and absu.startswith("http"):
                pd.links.append(absu.split("#")[0])

    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    pd.text = text
    for e in EMAIL_RE.findall(text):
        if not JUNK_EMAIL.search(e):
            pd.emails.add(e.lower().rstrip("."))
    return pd


def choose_pages(home: str, links: list[str]) -> list[str]:
    seen, picked = {home.rstrip("/")}, []
    for hint in PAGE_HINTS:
        for link in links:
            l = link.rstrip("/")
            path = urlsplit(l).path.lower()
            if l in seen or not path or re.search(r"\.(pdf|jpe?g|png|zip|mp4)$", path):
                continue
            if hint in path:
                seen.add(l)
                picked.append(l)
                break
        if len(picked) >= MAX_PAGES - 1:
            break
    return picked


def site_name_from(pd: PageData) -> str | None:
    for item in pd.jsonld:
        if isinstance(item.get("name"), str) and item.get("@type") not in ("WebPage", "BreadcrumbList"):
            return item["name"].strip()
    if pd.site_name:
        return pd.site_name
    if pd.title:
        return re.split(r"\s[|\-–—:]\s", pd.title)[0].strip() or None
    return None


# ---------------- LLM summary ----------------

class TeamMember(BaseModel):
    name: str
    role: str | None = None


class Snippet(BaseModel):
    quote: str = Field(description="exact text copied from the page, max 300 chars")
    source_url: str
    about: str = Field(description="what this snippet shows, e.g. services, audience, video, team")


class SiteSummary(BaseModel):
    business_name: str | None = None
    category: str | None = None
    services: list[str] = []
    audience: str | None = None
    tone: str | None = None
    team: list[TeamMember] = []
    country: str | None = None
    snippets: list[Snippet] = []


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_is_verbatim(quote: str, page_text: str) -> bool:
    q, t = _norm_text(quote), _norm_text(page_text)
    if len(q) < 8:
        return False
    return q in t or fuzz.partial_ratio(q, t) >= 92


def _store_raw(session: Session, lead: Lead, idx: int, html: str, cfg: Settings) -> None:
    d = Path(cfg.upload_dir) / "raw" / str(lead.id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{int(time.time())}_{idx}.html"
    p.write_text(html, encoding="utf-8")
    session.add(RawFile(lead_id=lead.id, kind="html", path=str(p)))


def enrich_lead(session: Session, lead: Lead, *, fetcher: PoliteFetcher | None = None, llm: LLM | None = None,
                cfg: Settings | None = None, actor: str = "system") -> dict:
    cfg = cfg or get_settings()
    if not lead.website:
        return {"ok": False, "reason": "lead has no website"}
    fetcher = fetcher or PoliteFetcher(cfg=cfg)
    pages: list[PageData] = []
    raw_html: list[str] = []
    home = lead.website if lead.website.startswith("http") else "https://" + lead.website
    html = fetcher.get(home)
    if html is None:
        return {"ok": False, "reason": "homepage not fetchable or disallowed by robots.txt"}
    raw_html.append(html)
    pages.append(parse_page(home, html))
    for url in choose_pages(home, pages[0].links):
        h = fetcher.get(url)
        if h:
            raw_html.append(h)
            pages.append(parse_page(url, h))
    for i, h in enumerate(raw_html):
        _store_raw(session, lead, i, h, cfg)

    org = session.get(Organization, lead.organization_id)
    added = {"contacts": 0, "evidence": 0}
    site_domain = normalize_domain(home)

    # --- deterministic extraction ---
    for pd in pages:
        for e in pd.emails:
            if add_contact_point(session, lead, "email", e, pd.url):
                added["contacts"] += 1
        for ph in pd.phones:
            if add_contact_point(session, lead, "phone", ph, pd.url):
                added["contacts"] += 1
        for platform, urls in pd.socials.items():
            for u in urls:
                try:
                    normalize_handle(platform, u)
                except ValueError:
                    continue
                if add_contact_point(session, lead, platform, u, pd.url):
                    added["contacts"] += 1
        for item in pd.jsonld:
            for key in ("email", "telephone"):
                v = item.get(key)
                if isinstance(v, str) and add_contact_point(session, lead, "email" if key == "email" else "phone",
                                                            v, pd.url):
                    added["contacts"] += 1
            addr = item.get("address")
            if isinstance(addr, dict) and org and not org.country:
                org.country = to_country_code(addr.get("addressCountry")) or org.country
            if isinstance(addr, dict) and org and not org.city and isinstance(addr.get("addressLocality"), str):
                org.city = addr["addressLocality"]
        if pd.video_embeds:
            add_evidence(session, lead, kind="website_text", source_type="web", source_url=pd.url,
                         snippet=f"Page embeds {pd.video_embeds} video player(s)", confidence=1.0)
            added["evidence"] += 1
    if org and not org.domain_norm:
        org.domain_norm = site_domain

    # --- LLM summary with verified snippets ---
    corpus = "\n\n".join(f"### PAGE {p.url}\nTITLE: {p.title}\n{p.text[:3500]}" for p in pages)[:16000]
    summary: SiteSummary | None = None
    if llm is not None:
        summary = llm.json(task="website_extract", model=cfg.model_extract, system=load_prompt("extract_system.md"),
                           content=corpus, schema=SiteSummary, max_tokens=1800)
    if summary:
        page_text = {p.url: p.text for p in pages}
        all_text = " ".join(page_text.values())
        rejected = 0
        for sn in summary.snippets[:12]:
            src = sn.source_url if sn.source_url in page_text else None
            if quote_is_verbatim(sn.quote, page_text.get(src, all_text)):
                add_evidence(session, lead, kind="website_text", source_type="web",
                             source_url=src or pages[0].url, snippet=sn.quote[:300], confidence=0.9)
                added["evidence"] += 1
            else:
                rejected += 1
        added["rejected_snippets"] = rejected
        if summary.services or summary.category:
            lead.description = "; ".join(filter(None, [summary.category, ", ".join(summary.services[:8]),
                                                       f"audience: {summary.audience}" if summary.audience else None,
                                                       f"tone: {summary.tone}" if summary.tone else None]))
        if summary.team and not lead.contact_name:
            # team names come from the business's own public team page (allowed); verify they appear
            for tm in summary.team:
                if quote_is_verbatim(tm.name, all_text):
                    lead.contact_name, lead.contact_role = tm.name, tm.role
                    break
        if org and not org.country and summary.country:
            org.country = to_country_code(summary.country)
        if org and summary.business_name and org.name.startswith("http"):
            org.name = summary.business_name

    if org:
        session.add(org)
    lead.updated_at = utcnow()
    session.add(lead)

    # EU/UK gate (plain code)
    emails = [p for pd in pages for p in pd.emails]
    if not cfg.allow_eu_uk and org and (is_eu_uk(org.country, site_domain) or any(is_eu_uk(None, e) for e in emails)):
        set_status(session, lead, LeadStatus.BLOCKED, actor=actor, reason="EU/UK recipient (ALLOW_EU_UK=false)")
        return {"ok": True, "pages": len(pages), **added, "blocked": "EU/UK"}

    if lead.status == LeadStatus.NEW:
        set_status(session, lead, LeadStatus.RESEARCHED, actor=actor, reason="website enriched")
    return {"ok": True, "pages": len(pages), **added}
