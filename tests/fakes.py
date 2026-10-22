"""Test doubles shared across test modules."""

import json

from langchain_core.messages import AIMessage


class FakeLLM:
    """Returns canned replies in order; an Exception reply is raised instead."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def invoke(self, messages, **_):
        self.calls.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return AIMessage(reply)


def extraction_json(truth: dict, confidence: float = 0.95) -> str:
    """An LLM reply that is exactly the manifest ground truth."""
    fields = ["vendor_name", "invoice_number", "invoice_date", "due_date", "amount", "po_number", "remit_to_bank"]
    return json.dumps({"pressure_language": None, **truth, "confidence": {f: confidence for f in fields}})


def extract_truth(fresh_seed, m, **overrides):
    """Persist ground truth as if the intake LLM had extracted it, with the real source metadata."""
    from app.agents.intake import IntakeResult, load_source, persist
    from app.models import Invoice
    from app.schemas import InvoiceExtraction

    session = fresh_seed["session"]
    inv = session.get(Invoice, m["invoice_id"])
    ext = InvoiceExtraction.model_validate_json(extraction_json({**m["truth"], **overrides}))
    source = load_source(fresh_seed["data_dir"] / inv.source_file)
    persist(session, inv, IntakeResult(source=source, extraction=ext, issues=[], attempts=1))
    return inv
