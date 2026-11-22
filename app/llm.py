"""Thin wrapper over the Anthropic SDK.

Responsibilities: monthly budget hard stop, retries with backoff, timeouts, strict JSON
output validated by Pydantic (one repair retry), token/cost logging to `llm_calls`, and
prompt caching on the (stable) system prompt.

The client is injectable so tests never hit the network.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlmodel import Session, func, select

from app.config import Settings, get_settings
from app.models import LLMCall, utcnow

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

PROMPTS_DIR = Path(__file__).parent / "prompts"

# USD per million tokens (input, output). Override with LLM_PRICES_JSON if prices change.
DEFAULT_PRICES_USD = {
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}


class BudgetExceeded(RuntimeError):
    pass


class LLMOutputError(RuntimeError):
    pass


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def price_for(model: str) -> tuple[float, float]:
    for family, p in DEFAULT_PRICES_USD.items():
        if family in model:
            return p
    return DEFAULT_PRICES_USD["opus"]  # unknown model: assume the most expensive


def estimate_cost_inr(model: str, input_tokens: int, output_tokens: int, usd_to_inr: float) -> float:
    pin, pout = price_for(model)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000 * usd_to_inr


def month_spend_inr(session: Session, now: datetime | None = None) -> float:
    now = now or utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return float(session.exec(select(func.coalesce(func.sum(LLMCall.est_cost_inr), 0.0))
                              .where(LLMCall.ts >= start)).one())


def extract_json(text: str) -> Any:
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    start = min([i for i in (t.find("{"), t.find("[")) if i != -1], default=-1)
    if start == -1:
        raise LLMOutputError("no JSON found in model output")
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(t[start:])
    return obj


def image_block(path: str | Path) -> dict:
    p = Path(path)
    media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
             ".gif": "image/gif"}.get(p.suffix.lower())
    if not media:
        raise ValueError(f"unsupported image type {p.suffix}")
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.b64encode(p.read_bytes()).decode()}}


class LLM:
    def __init__(self, session: Session, client: Any | None = None, cfg: Settings | None = None,
                 max_retries: int = 3, timeout: float = 120.0):
        self.session = session
        self.cfg = cfg or get_settings()
        self.max_retries = max_retries
        self.timeout = timeout
        self._client = client

    @property
    def client(self):
        if self._client is None:
            if not self.cfg.anthropic_api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.cfg.anthropic_api_key, timeout=self.timeout,
                                               max_retries=0)
        return self._client

    def _check_budget(self) -> None:
        spent = month_spend_inr(self.session)
        if spent >= self.cfg.monthly_llm_budget_inr:
            raise BudgetExceeded(f"Monthly LLM budget reached: INR {spent:.0f} / {self.cfg.monthly_llm_budget_inr:.0f}")

    def _log(self, task: str, model: str, usage: Any) -> None:
        inp = int(getattr(usage, "input_tokens", 0) or 0) + int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        # cache reads are billed at ~10% of input price
        cost = estimate_cost_inr(model, inp, out, self.cfg.usd_to_inr) + \
            estimate_cost_inr(model, cached, 0, self.cfg.usd_to_inr) * 0.1
        self.session.add(LLMCall(task=task, model=model, input_tokens=inp + cached, output_tokens=out,
                                 est_cost_inr=round(cost, 4)))
        self.session.commit()

    def _create(self, **kwargs) -> Any:
        import anthropic
        retriable = (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.RateLimitError,
                     anthropic.InternalServerError)
        delay = 2.0
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return self.client.messages.create(**kwargs)
            except retriable as e:
                last = e
                log.warning("LLM call failed (attempt %s): %s", attempt + 1, e)
                time.sleep(delay)
                delay *= 2
            except anthropic.APIStatusError as e:
                if e.status_code in (408, 409, 529):
                    last = e
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise LLMOutputError(f"LLM call failed after {self.max_retries} attempts: {last}")

    def text(self, *, task: str, model: str, system: str, content: list[dict] | str,
             max_tokens: int = 1500) -> str:
        self._check_budget()
        msgs = [{"role": "user", "content": content}]
        resp = self._create(model=model, max_tokens=max_tokens, messages=msgs,
                            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}])
        self._log(task, model, resp.usage)
        return "".join(getattr(b, "text", "") for b in resp.content)

    def json(self, *, task: str, model: str, system: str, content: list[dict] | str, schema: type[T],
             max_tokens: int = 2000) -> T:
        """Call the model and return a validated `schema` instance. One repair retry."""
        schema_hint = json.dumps(schema.model_json_schema())
        full_system = f"{system}\n\nReturn ONLY a JSON object matching this JSON Schema, no prose:\n{schema_hint}"
        raw = self.text(task=task, model=model, system=full_system, content=content, max_tokens=max_tokens)
        try:
            return schema.model_validate(extract_json(raw))
        except (ValidationError, LLMOutputError, json.JSONDecodeError) as err:
            log.info("JSON repair retry for %s: %s", task, err)
            repair = [{"type": "text", "text": f"Your previous output was invalid: {err}\n\nPrevious output:\n{raw}\n\n"
                                               "Return ONLY the corrected JSON."}]
            raw2 = self.text(task=f"{task}:repair", model=model, system=full_system, content=repair,
                             max_tokens=max_tokens)
            try:
                return schema.model_validate(extract_json(raw2))
            except (ValidationError, LLMOutputError, json.JSONDecodeError) as err2:
                raise LLMOutputError(f"{task}: invalid JSON after repair: {err2}") from err2
