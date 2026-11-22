import pytest
from pydantic import BaseModel
from sqlmodel import select

from app.llm import LLM, BudgetExceeded, LLMOutputError, estimate_cost_inr, extract_json, month_spend_inr
from app.models import LLMCall


class Out(BaseModel):
    label: str
    score: float


def test_json_call_logs_cost(session, fake_llm, cfg):
    fake_llm.queue('```json\n{"label": "ok", "score": 0.9}\n```')
    out = LLM(session, client=fake_llm, cfg=cfg).json(task="t", model="claude-haiku-5-5", system="s",
                                                       content="hi", schema=Out)
    assert out.label == "ok"
    call = session.exec(select(LLMCall)).one()
    assert call.task == "t" and call.input_tokens == 1000 and call.est_cost_inr > 0
    # system prompt uses prompt caching
    assert fake_llm.requests[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_json_repair_retry(session, fake_llm, cfg):
    fake_llm.queue('{"label": "ok"}', '{"label": "ok", "score": 1}')
    out = LLM(session, client=fake_llm, cfg=cfg).json(task="t", model="m-sonnet", system="s", content="x", schema=Out)
    assert out.score == 1
    assert len(session.exec(select(LLMCall)).all()) == 2


def test_json_fails_after_repair(session, fake_llm, cfg):
    fake_llm.queue("nope", "still nope")
    with pytest.raises(LLMOutputError):
        LLM(session, client=fake_llm, cfg=cfg).json(task="t", model="m", system="s", content="x", schema=Out)


def test_budget_hard_stop(session, fake_llm, cfg):
    cfg.monthly_llm_budget_inr = 10
    session.add(LLMCall(task="old", model="m", est_cost_inr=10.5))
    session.commit()
    assert month_spend_inr(session) >= 10
    with pytest.raises(BudgetExceeded):
        LLM(session, client=fake_llm, cfg=cfg).text(task="t", model="m", system="s", content="x")
    assert fake_llm.requests == []  # never called the API


def test_extract_json_with_prose():
    assert extract_json('Here you go: {"a": [1, 2]} trailing') == {"a": [1, 2]}


def test_cost_estimate_routes_by_family():
    assert estimate_cost_inr("claude-opus-5-5", 1_000_000, 0, 1.0) > estimate_cost_inr("claude-haiku-5-5", 1_000_000, 0, 1.0)
