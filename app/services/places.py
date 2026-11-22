"""Google Places API (New) discovery — spec 6.1.

Places content (name, phone, address, website, coordinates) is TRANSIENT here: we persist only
`google_place_id`. The website URL is used in-session to visit the business's own site; the
organisation name and all contact details are then taken from that site.

Gated by PLACES_DISCOVERY_ENABLED: set it only after reading the current Google Maps Platform
Service Specific Terms (Places caching table) and confirming that this use case is permitted.
"""
from __future__ import annotations

import logging

import httpx
from sqlmodel import Session, select

from app.audit import audit
from app.config import Settings, get_settings
from app.models import DiscoveryJob, Niche, Organization
from app.services.enrichment import PoliteFetcher, parse_page, site_name_from
from app.services.leads import create_lead
from app.services.niches import niche_config
from app.services.normalize import normalize_domain

log = logging.getLogger(__name__)
SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{id}"
# Minimal mask first; contact-level fields only for places that pass the cheap filter
SEARCH_MASK = "places.id,places.displayName,places.location,places.primaryType,places.types,nextPageToken"
DETAILS_MASK = "id,websiteUri"


class PlacesDisabled(RuntimeError):
    pass


class PlacesClient:
    def __init__(self, api_key: str, client: httpx.Client | None = None):
        if not api_key:
            raise PlacesDisabled("GOOGLE_PLACES_API_KEY is not set")
        self.key = api_key
        self.http = client or httpx.Client(timeout=20.0)

    def text_search(self, query: str, *, lat: float | None = None, lng: float | None = None,
                    radius_m: int = 5000, page_token: str | None = None) -> dict:
        body: dict = {"textQuery": query, "pageSize": 20}
        if lat is not None and lng is not None:
            body["locationBias"] = {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": float(radius_m)}}
        if page_token:
            body["pageToken"] = page_token
        r = self.http.post(SEARCH_URL, json=body, headers={"X-Goog-Api-Key": self.key, "X-Goog-FieldMask": SEARCH_MASK})
        r.raise_for_status()
        return r.json()

    def website(self, place_id: str) -> str | None:
        r = self.http.get(DETAILS_URL.format(id=place_id),
                          headers={"X-Goog-Api-Key": self.key, "X-Goog-FieldMask": DETAILS_MASK})
        r.raise_for_status()
        return r.json().get("websiteUri")


def _cheap_filter(place: dict, wanted_words: list[str]) -> bool:
    """Category sanity check on minimal fields before paying for details."""
    if not wanted_words:
        return True
    hay = " ".join([place.get("primaryType") or "", *place.get("types", []),
                    (place.get("displayName") or {}).get("text", "")]).lower().replace("_", " ")
    return any(w in hay for w in wanted_words)


_FILTER_WORDS = {"medspa": ["spa", "skin", "aesthetic", "beauty", "laser", "clinic", "dermatolog"],
                 "clinic": ["clinic", "dental", "dentist", "physio", "doctor", "health", "dermatolog"],
                 "realtor": ["real estate", "realtor", "property", "estate"],
                 "agency": ["marketing", "agency", "advertising", "media"]}


def run_discovery(session: Session, job: DiscoveryJob, *, places: PlacesClient | None = None,
                  fetcher: PoliteFetcher | None = None, cfg: Settings | None = None) -> DiscoveryJob:
    cfg = cfg or get_settings()
    if not cfg.places_discovery_enabled:
        raise PlacesDisabled("Places discovery is disabled. Review the Places caching terms, then set "
                             "PLACES_DISCOVERY_ENABLED=true — or use CSV import instead.")
    places = places or PlacesClient(cfg.google_places_api_key)
    fetcher = fetcher or PoliteFetcher(cfg=cfg)
    niche = session.get(Niche, job.niche_id) if job.niche_id else None
    ncfg = niche_config(niche)
    queries = ncfg.get("search_queries") or [niche.name if niche else "business"]
    words = _FILTER_WORDS.get(niche.slug if niche else "", [])
    job.status = "RUNNING"
    session.add(job)
    session.commit()
    try:
        seen: set[str] = set()
        for q in queries:
            if job.found >= job.max_results:
                break
            data = places.text_search(f"{q} in {job.city}", radius_m=job.radius_m)
            for place in data.get("places", []):
                if job.found >= job.max_results:
                    break
                pid = place.get("id")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                job.found += 1
                if session.exec(select(Organization.id).where(Organization.google_place_id == pid)).first():
                    job.skipped += 1
                    continue
                if not _cheap_filter(place, words):
                    job.skipped += 1
                    continue
                website = places.website(pid)  # transient
                if not website:
                    job.skipped += 1  # nothing we may store without the business's own site
                    continue
                html = fetcher.get(website)
                if html is None:
                    job.skipped += 1
                    continue
                name = site_name_from(parse_page(website, html)) or normalize_domain(website)
                res = create_lead(session, name=name, website=website, city=job.city, niche_id=job.niche_id,
                                  place_id=pid, source="places")
                if res.lead:
                    job.created += 1
                else:
                    job.skipped += 1
                session.commit()
        job.status = "DONE"
    except Exception as e:  # record and surface
        log.exception("discovery job %s failed", job.id)
        job.status, job.error = "FAILED", str(e)[:500]
    audit(session, "discovery.run", entity="discovery_job", entity_id=job.id, status=job.status,
          found=job.found, created=job.created)
    session.add(job)
    session.commit()
    return job
