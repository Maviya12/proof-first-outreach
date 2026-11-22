"""The golden set is well-formed and the harness computes metrics (with a fake LLM).
Real evaluation: `python -m app.eval_golden` with ANTHROPIC_API_KEY set."""
import json
import re

from app.eval_golden import GOLDEN, run
from app.llm import LLM


def test_golden_file_shape():
    leads = json.loads(GOLDEN.read_text())
    assert len(leads) == 30
    assert {g["niche"] for g in leads} == {"medspa", "clinic", "realtor", "creator", "agency"}
    assert all(len(g["evidence"]) >= 2 for g in leads)


def test_harness_metrics_with_fake_llm(session, fake_llm, cfg):
    def respond(req):
        content = req["messages"][0]["content"]
        system = req["system"][0]["text"]
        if "audit an outreach message" in system:
            return {"items": [{"text": "x", "supported": True, "evidence_ids": [1]}]}
        if "review two outreach drafts" in system:
            return {"score_a": 7, "score_b": 6, "selected": "a", "reason": "ok"}
        ids = [int(i) for i in re.findall(r"^\[(\d+)\]", content, re.M)]
        url = re.search(r"include exactly: .+? — (\S+)", content).group(1)
        first = re.search(r"^\[\d+\] \([^)]*\)[^:]*: (.+)$", content, re.M).group(1)
        dm = "Channel: email" not in content
        filler = ("I edit short-form video for teams like yours, with clean captions, tight openings and a "
                  "consistent look across posts, usually turned around within 48 hours. ")
        body = (f"Hi there, I saw this: {first} {filler}"
                + ("" if dm else "I keep things simple and collaborative, and I'm happy to work around your schedule and brand guidelines. ")
                + f"Samples: {url} Would a short sample plan be useful?")
        d = {"subject": None if dm else "Editing help", "body": body, "claims": [{"text": first, "evidence_ids": [ids[0]]}]}
        return {"draft_a": d, "draft_b": d}

    fake_llm.queue(*[respond] * 200)
    report = run(session, LLM(session, client=fake_llm, cfg=cfg), cfg=cfg)
    assert report["leads"] == 30 and report["messages"] == 30
    assert report["banned_phrase_rate"] == 0.0 and report["unsupported_claim_rate"] == 0.0
    assert report["drafts"] == 60
