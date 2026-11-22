"""In-process APScheduler. Run exactly ONE app worker so jobs don't run twice."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from app.db import get_engine

log = logging.getLogger(__name__)


def _run(name: str, fn) -> None:  # noqa: ANN001
    try:
        with Session(get_engine()) as s:
            result = fn(s)
            s.commit()
            if result:
                log.info("job %s: %s", name, result)
    except Exception:  # keep the scheduler alive
        log.exception("job %s failed", name)


def job_retention(session: Session):
    from app.services.privacy import purge_raw_files, purge_stale_leads
    return {"raw": purge_raw_files(session), "leads": purge_stale_leads(session)}


def job_dispatch_due(session: Session):
    from app.services.dispatcher import dispatch_due
    return dispatch_due(session)


def job_reply_sync(session: Session):
    from app.services.reply_sync import sync_gmail_replies
    return sync_gmail_replies(session)


def job_followups(session: Session):
    from app.services.followups import plan_followups
    return plan_followups(session)


def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1})
    sched.add_job(lambda: _run("dispatch_due", job_dispatch_due), "interval", minutes=2, id="dispatch_due")
    sched.add_job(lambda: _run("reply_sync", job_reply_sync), "interval", minutes=5, id="reply_sync")
    sched.add_job(lambda: _run("followups", job_followups), "interval", hours=1, id="followups")
    sched.add_job(lambda: _run("retention", job_retention), "cron", hour=21, minute=30, id="retention")
    sched.start()
    log.info("scheduler started")
    return sched
