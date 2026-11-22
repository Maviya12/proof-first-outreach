"""YouTube Data API v3 public metadata (Phase 6).

The user supplies a channel URL; we read public metadata only (video count, recent upload
titles/dates/durations) and store checkable facts as evidence. No scraping, no views/subscriber
"performance" judgements.
"""
from __future__ import annotations

import re
from datetime import datetime

import httpx
from sqlmodel import Session

from app.config import Settings, get_settings
from app.models import Lead
from app.services.leads import add_contact_point, add_evidence
from app.services.normalize import normalize_handle

API = "https://www.googleapis.com/youtube/v3"


def iso_duration_seconds(d: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    days, h, mi, s = (int(x or 0) for x in m.groups())
    return days * 86400 + h * 3600 + mi * 60 + s


class YouTubeClient:
    def __init__(self, api_key: str, client: httpx.Client | None = None):
        if not api_key:
            raise RuntimeError("YOUTUBE_API_KEY is not set")
        self.key, self.http = api_key, client or httpx.Client(timeout=20.0)

    def _get(self, path: str, **params) -> dict:
        r = self.http.get(f"{API}/{path}", params={**params, "key": self.key})
        r.raise_for_status()
        return r.json()

    def channel(self, handle: str) -> dict | None:
        if handle.startswith("channel/"):
            data = self._get("channels", part="snippet,statistics,contentDetails", id=handle.split("/", 1)[1])
        else:
            data = self._get("channels", part="snippet,statistics,contentDetails", forHandle=handle)
        items = data.get("items", [])
        return items[0] if items else None

    def recent_videos(self, uploads_playlist: str, n: int = 10) -> list[dict]:
        pl = self._get("playlistItems", part="contentDetails", playlistId=uploads_playlist, maxResults=n)
        ids = [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
        if not ids:
            return []
        return self._get("videos", part="snippet,contentDetails", id=",".join(ids)).get("items", [])


def add_youtube_evidence(session: Session, lead: Lead, url: str, *, yt: YouTubeClient | None = None,
                         cfg: Settings | None = None) -> dict:
    cfg = cfg or get_settings()
    handle = normalize_handle("youtube", url)
    yt = yt or YouTubeClient(cfg.youtube_api_key)
    ch = yt.channel(handle)
    if not ch:
        raise ValueError("channel not found")
    add_contact_point(session, lead, "youtube", url, source_url=url)
    src = f"https://www.youtube.com/{handle}"
    n_ev = 0
    count = (ch.get("statistics") or {}).get("videoCount")
    if count:
        add_evidence(session, lead, kind="youtube_meta", source_type="api", source_url=src,
                     snippet=f"YouTube channel has {count} public videos")
        n_ev += 1
    vids = yt.recent_videos(ch["contentDetails"]["relatedPlaylists"]["uploads"])
    if vids:
        shorts = sum(1 for v in vids if iso_duration_seconds(v["contentDetails"]["duration"]) <= 60)
        dates = sorted(datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00")).date() for v in vids)
        add_evidence(session, lead, kind="youtube_meta", source_type="api", source_url=src,
                     snippet=f"Of the last {len(vids)} uploads, {shorts} are Shorts (60 s or less); "
                             f"most recent upload on {dates[-1].isoformat()}")
        n_ev += 1
        for v in vids[:3]:
            add_evidence(session, lead, kind="youtube_meta", source_type="api",
                         source_url=f"https://www.youtube.com/watch?v={v['id']}",
                         snippet=f"Recent video title: \"{v['snippet']['title'][:150]}\"")
            n_ev += 1
    return {"channel": ch["snippet"]["title"], "evidence_added": n_ev}
