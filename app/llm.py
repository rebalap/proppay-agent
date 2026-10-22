"""ChatOllama factory + structured-output helper.

Contract (CLAUDE.md): every LLM output is parsed into a Pydantic model. On parse
failure we retry once with the validation error fed back; if that also fails the
caller gets ok=False and must route to human review. We never guess or repair.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any, Generic, Protocol, TypeVar

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from app.config import get_settings

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
MAX_PARSE_ATTEMPTS = 2  # first try + one retry

T = TypeVar("T", bound=BaseModel)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


class ChatModel(Protocol):
    def invoke(self, input: list[BaseMessage], **kwargs: Any) -> BaseMessage: ...


def get_llm(*, json_schema: dict | None = None, **overrides: Any):
    """Build a ChatOllama. With json_schema, Ollama constrains decoding to that schema."""
    from langchain_ollama import ChatOllama

    s = get_settings()
    kwargs: dict[str, Any] = dict(
        model=s.ollama_model,
        base_url=s.ollama_base_url,
        temperature=s.llm_temperature,
        num_ctx=s.llm_num_ctx,
        reasoning=False,  # qwen3: skip <think> output; we only want the JSON
    )
    if json_schema is not None:
        kwargs["format"] = json_schema
    kwargs.update(overrides)
    return ChatOllama(**kwargs)


@lru_cache
def load_prompt(name: str) -> Template:
    """Load app/prompts/<name>.txt. Placeholders use $var (JSON braces stay literal)."""
    return Template((PROMPTS_DIR / f"{name}.txt").read_text())


def render_prompt(name: str, **values: Any) -> str:
    return load_prompt(name).substitute(**values)


@dataclass
class StructuredResult(Generic[T]):
    ok: bool
    value: T | None = None
    attempts: int = 0
    error: str | None = None
    raw_outputs: list[str] = field(default_factory=list)

    @property
    def needs_human_review(self) -> bool:
        return not self.ok


def _clean(text: str) -> str:
    text = _THINK_RE.sub("", text).strip()
    return _FENCE_RE.sub("", text).strip()


def structured_call(
    schema: type[T],
    *,
    system: str,
    user: str,
    llm: ChatModel | None = None,
) -> StructuredResult[T]:
    """Ask the LLM for JSON matching `schema`. Retry once on parse/validation failure."""
    if llm is None:
        llm = get_llm(json_schema=schema.model_json_schema())

    messages: list[BaseMessage] = [
        SystemMessage(
            system
            + "\n\nRespond with a single JSON object that matches this JSON schema. No prose.\n"
            + json.dumps(schema.model_json_schema())
        ),
        HumanMessage(user),
    ]
    result: StructuredResult[T] = StructuredResult(ok=False)

    for attempt in range(1, MAX_PARSE_ATTEMPTS + 1):
        result.attempts = attempt
        try:
            reply = llm.invoke(messages)
        except Exception as exc:  # transport errors: don't retry blindly, surface them
            result.error = f"llm_error: {type(exc).__name__}: {exc}"
            log.warning("LLM call failed for %s: %s", schema.__name__, result.error)
            return result

        raw = reply.content if isinstance(reply.content, str) else json.dumps(reply.content)
        result.raw_outputs.append(raw)
        try:
            result.value = schema.model_validate_json(_clean(raw))
            result.ok = True
            result.error = None
            return result
        except ValidationError as exc:
            result.error = f"validation_error: {exc.errors(include_url=False)}"
            log.info("Parse attempt %d for %s failed: %s", attempt, schema.__name__, result.error)
            messages += [
                AIMessage(raw),
                HumanMessage(
                    "Your previous reply did not validate against the schema. Errors:\n"
                    f"{exc}\nReturn only the corrected JSON object."
                ),
            ]

    return result
