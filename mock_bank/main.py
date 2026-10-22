"""Mock bank (SPEC §6.7). No real money; outcomes for demo cases are seeded in
data/mock_bank_scenarios.json.

    uv run uvicorn mock_bank.main:app --port 8001

Idempotency: every payment instruction carries an Idempotency-Key. Same key + same body
replays the original response; same key + a different body is a 409 (a client bug).
ACH returns are not revealed on submission; like a real bank they show up later on the
statement (2 business days after the effective date).
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from app.payments import nacha
from app.payments.routing import is_valid_aba
from app.payments.vault import hash_account
from app.schemas import to_cents

# Simplified BAI2 type codes used on our operating-account statement.
BAI_TYPE = {"ach": "451", "wire": "495", "rtp": "495", "fednow": "495", "check": "475", "card": "699",
            "ach_return": "166"}
ROOT = Path(__file__).resolve().parent.parent


def _business_days(d: date, n: int) -> date:
    while n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


class InstantIn(BaseModel):
    rail: Literal["rtp", "fednow"]
    amount_cents: int
    creditor_routing: str
    creditor_account: str
    creditor_name: str
    reference: str
    value_date: date


class WireIn(BaseModel):
    amount_cents: int
    beneficiary_routing: str
    beneficiary_account: str
    beneficiary_name: str
    reference: str
    value_date: date


class CardIn(BaseModel):
    amount_cents: int
    merchant_name: str
    reference: str
    value_date: date


class PositivePayIn(BaseModel):
    csv: str


class AccountValidationIn(BaseModel):
    routing: str
    account_number: str


class TinMatchIn(BaseModel):
    tin: str
    name: str


class Bank:
    def __init__(self, scenarios: dict, state_path: Path | None):
        self.scenarios = scenarios
        self.state_path = state_path
        self.lock = threading.Lock()
        self.state = {"idempotency": {}, "transactions": [], "ach_files": {}, "positive_pay": []}
        if state_path and state_path.exists():
            self.state = json.loads(state_path.read_text())

    def save(self) -> None:
        if self.state_path:
            self.state_path.write_text(json.dumps(self.state, indent=1, default=str))

    def returns_for(self, account_number: str) -> str | None:
        h = hash_account(account_number)
        return next((r["return_code"] for r in self.scenarios.get("ach_returns", []) if r["account_hash"] == h), None)

    def idempotent(self, key: str | None, body: dict, make) -> JSONResponse:
        if not key:
            raise HTTPException(400, "Idempotency-Key header is required")
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
        with self.lock:
            seen = self.state["idempotency"].get(key)
            if seen:
                if seen["digest"] != digest:
                    raise HTTPException(409, "Idempotency-Key reused with a different request body")
                return JSONResponse(seen["response"], headers={"Idempotent-Replayed": "true"})
            response = make()
            self.state["idempotency"][key] = {"digest": digest, "response": response}
            self.save()
            return JSONResponse(response, status_code=201)

    def post_txn(self, **txn) -> dict:
        txn = {"id": f"TX{len(self.state['transactions']) + 1:06d}", **txn}
        self.state["transactions"].append(txn)
        return txn


def create_app(scenarios_path: Path | None = None, state_path: Path | None = None) -> FastAPI:
    scenarios = json.loads(scenarios_path.read_text()) if scenarios_path and scenarios_path.exists() else {}
    bank = Bank(scenarios, state_path)
    api = FastAPI(title="PropPay mock bank")
    api.state.bank = bank

    @api.post("/ach/files")
    async def ach_file(request: Request, idempotency_key: str | None = Header(None)):
        text = (await request.body()).decode()
        entries, errors = nacha.parse_and_validate(text)
        if errors:
            raise HTTPException(422, {"errors": errors})
        file_hash = hashlib.sha256(text.encode()).hexdigest()

        def accept():
            if file_hash in bank.state["ach_files"]:  # the same file twice is a duplicate, not a new payment run
                return bank.state["ach_files"][file_hash]
            file_id = f"ACH{len(bank.state['ach_files']) + 1:05d}"
            for e in entries:
                ref = f"{file_id}-{e.trace}"
                bank.post_txn(kind="ach", type_code=BAI_TYPE["ach"], direction="debit", amount_cents=e.amount_cents,
                              value_date=e.effective_date.isoformat(), bank_reference=ref,
                              customer_reference=e.individual_id, routing=e.routing,
                              account_last4=e.account_number[-4:])
                code = bank.returns_for(e.account_number)
                if code:
                    bank.post_txn(kind="ach_return", type_code=BAI_TYPE["ach_return"], direction="credit",
                                  amount_cents=e.amount_cents,
                                  value_date=_business_days(e.effective_date, 2).isoformat(),
                                  bank_reference=f"RET-{ref}", customer_reference=e.individual_id,
                                  return_code=code, routing=e.routing, account_last4=e.account_number[-4:])
            resp = {"file_id": file_id, "status": "accepted", "batches": len({e.batch_number for e in entries}),
                    "entries": len(entries), "total_credit_cents": sum(e.amount_cents for e in entries)}
            bank.state["ach_files"][file_hash] = resp
            return resp

        return bank.idempotent(idempotency_key or f"file-{file_hash}", {"file_sha256": file_hash}, accept)

    @api.post("/instant")
    def instant(body: InstantIn, idempotency_key: str | None = Header(None)):
        def pay():
            if not is_valid_aba(body.creditor_routing):
                return {"status": "rejected", "reason": "invalid creditor routing number"}
            txn = bank.post_txn(kind=body.rail, type_code=BAI_TYPE[body.rail], direction="debit",
                                amount_cents=body.amount_cents, value_date=body.value_date.isoformat(),
                                bank_reference=f"{body.rail.upper()}-{uuid.uuid4().hex[:12].upper()}",
                                customer_reference=body.reference, routing=body.creditor_routing,
                                account_last4=body.creditor_account[-4:])
            return {"status": "settled", "rail": body.rail, "bank_reference": txn["bank_reference"],
                    "settled_on": txn["value_date"]}
        return bank.idempotent(idempotency_key, body.model_dump(mode="json"), pay)

    @api.post("/wire")
    def wire(body: WireIn, idempotency_key: str | None = Header(None)):
        def pay():
            txn = bank.post_txn(kind="wire", type_code=BAI_TYPE["wire"], direction="debit",
                                amount_cents=body.amount_cents, value_date=body.value_date.isoformat(),
                                bank_reference=f"IMAD-{uuid.uuid4().hex[:12].upper()}",
                                customer_reference=body.reference, routing=body.beneficiary_routing,
                                account_last4=body.beneficiary_account[-4:])
            return {"status": "accepted", "bank_reference": txn["bank_reference"], "value_date": txn["value_date"]}
        return bank.idempotent(idempotency_key, body.model_dump(mode="json"), pay)

    @api.post("/card")
    def card(body: CardIn, idempotency_key: str | None = Header(None)):
        def pay():
            txn = bank.post_txn(kind="card", type_code=BAI_TYPE["card"], direction="debit",
                                amount_cents=body.amount_cents, value_date=body.value_date.isoformat(),
                                bank_reference=f"VC-{uuid.uuid4().hex[:12].upper()}",
                                customer_reference=body.reference)
            return {"status": "authorized", "bank_reference": txn["bank_reference"],
                    "card_last4": f"{int(hashlib.sha256(body.reference.encode()).hexdigest(), 16) % 10000:04d}"}
        return bank.idempotent(idempotency_key, body.model_dump(mode="json"), pay)

    @api.post("/positive-pay")
    def positive_pay(body: PositivePayIn, idempotency_key: str | None = Header(None)):
        def register():
            rows = body.csv.strip().splitlines()[1:]
            for row in rows:
                account, number, amount, issued, payee = row.split(",", 4)
                cents = to_cents(amount)
                bank.state["positive_pay"].append({"check_number": number, "amount_cents": cents, "payee": payee})
                bank.post_txn(kind="check", type_code=BAI_TYPE["check"], direction="debit", amount_cents=cents,
                              value_date=_business_days(date.fromisoformat(issued), 5).isoformat(),
                              bank_reference=f"CHK{number}", customer_reference=number)
            return {"status": "registered", "checks": len(rows)}
        return bank.idempotent(idempotency_key, body.model_dump(), register)

    @api.get("/statement")
    def statement(date_: date = Query(alias="date"), fmt: Literal["json", "bai2"] = Query("bai2", alias="format")):
        lines = [t for t in bank.state["transactions"] if t["value_date"] == date_.isoformat()]
        if fmt == "json":
            return lines
        d = date_.strftime("%y%m%d")
        acct = bank.scenarios.get("operating_account", "000123450001")
        recs = [f"01,PROPPAYMOCK,BLUESTEM,{d},0600,1,,,2/", f"02,BLUESTEM,099999999,1,{d},,USD,2/",
                f"03,{acct},USD/"]
        for t in lines:
            recs.append(f"16,{t['type_code']},{t['amount_cents']},0,{t['bank_reference']},"
                        f"{t['customer_reference']}/" + (f"\n88,RETURN {t['return_code']}/" if t.get("return_code") else ""))
        body = [r for rec in recs[2:] for r in rec.split("\n")]
        total = sum(t["amount_cents"] for t in lines)
        recs.append(f"49,{total},{len(body) + 1}/")
        # BAI2 counts: 49 = account records incl. 03 and 49; 98 = group incl. 02/98; 99 = whole file incl. 01/99
        recs += [f"98,{total},1,{len(body) + 3}/", f"99,{total},1,{len(body) + 5}/"]
        return PlainTextResponse("\n".join(recs) + "\n")

    @api.get("/health")
    def health():
        return {"status": "ok", "transactions": len(bank.state["transactions"])}

    @api.post("/admin/reload")
    def reload_state():
        """Re-read the state file (scripts/prepare_demo.py --restore swaps in a snapshot)."""
        with bank.lock:
            if state_path and state_path.exists():
                bank.state = json.loads(state_path.read_text())
        return {"status": "reloaded", "transactions": len(bank.state["transactions"])}

    @api.post("/admin/reset")
    def reset():
        """Forget all payments (called by scripts/seed_data.py so a reseed doesn't need a restart)."""
        with bank.lock:
            bank.scenarios = json.loads(scenarios_path.read_text()) if scenarios_path and scenarios_path.exists() else {}
            bank.state = {"idempotency": {}, "transactions": [], "ach_files": {}, "positive_pay": []}
            bank.save()
        return {"status": "reset"}

    @api.post("/account-validation")
    def account_validation(body: AccountValidationIn):
        if not is_valid_aba(body.routing):
            return {"result": "invalid", "reason": "routing number fails ABA checksum"}
        override = bank.scenarios.get("account_validation", {}).get(hash_account(body.account_number))
        return {"result": override or bank.scenarios.get("account_validation", {}).get("default", "valid")}

    @api.post("/tin-match")
    def tin_match(body: TinMatchIn):
        return {"result": bank.scenarios.get("tin_match", {}).get("default", "matched")}

    return api


app = create_app(ROOT / "data" / "mock_bank_scenarios.json", ROOT / "data" / "mock_bank_state.json")
