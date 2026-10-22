"""Rail & timing optimizer (SPEC §6.5). Pure and deterministic; economics from config/rails.yaml.

1. Filter eligible rails: vendor enrollment (card also needs card acceptance), amount limits,
   and instant rails only when the payment is urgent AND risk is low (they're irrevocable).
2. Pick the target settlement date: the early-pay discount deadline when the discount beats
   our cost of capital, otherwise the due date.
3. For each eligible rail, send as late as possible while still settling by the target date
   (keeps cash in the operating account), then choose the cheapest net cost (fee - rebate).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from pydantic import BaseModel

from app.config import rails_config

INSTANT_RAILS = {"rtp", "fednow"}
TERMS_RE = re.compile(r"(?P<pct>\d+(?:\.\d+)?)\s*%?\s*/?\s*(?P<disc>\d+)\s*,?\s*net\s*(?P<net>\d+)", re.I)
NET_RE = re.compile(r"net\s*(?P<net>\d+)", re.I)


class Terms(BaseModel):
    discount_pct: float = 0
    discount_days: int = 0
    net_days: int | None = None


class RailOption(BaseModel):
    rail: str
    eligible: bool
    reason: str
    send_date: date | None = None
    settle_date: date | None = None
    fee_cents: int = 0
    rebate_cents: int = 0
    meets_target: bool = False

    @property
    def net_cost_cents(self) -> int:
        return self.fee_cents - self.rebate_cents


class RailPlan(BaseModel):
    rail: str | None
    send_date: date | None
    settle_date: date | None
    target_date: date
    urgent: bool
    on_time: bool
    fee_cents: int = 0
    rebate_cents: int = 0
    discount_cents: int = 0
    pay_amount_cents: int
    reasons: list[str]
    options: list[RailOption]


# --- dates ---------------------------------------------------------------------------


def is_business_day(d: date) -> bool:
    return d.weekday() < 5


def next_business_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def add_business_days(d: date, n: int) -> date:
    for _ in range(n):
        d = next_business_day(d)
    return d


def _cutoff(cfg: dict) -> time | None:
    return time.fromisoformat(cfg["cutoff"]) if cfg.get("cutoff") else None


def _always_on(rail: str, cfg: dict) -> bool:
    return bool(cfg.get("always_on")) or rail == "virtual_card"


def earliest_send(rail: str, cfg: dict, now: datetime) -> date:
    today = now.date()
    if _always_on(rail, cfg):
        return today
    cutoff = _cutoff(cfg)
    if is_business_day(today) and (cutoff is None or now.time() <= cutoff):
        return today
    return next_business_day(today)


def settle_date_for(rail: str, cfg: dict, send: date) -> date:
    if _always_on(rail, cfg):
        return send
    return add_business_days(send, cfg.get("settle_days", 0)) if cfg.get("settle_days") else send


def latest_send(rail: str, cfg: dict, earliest: date, target: date) -> date | None:
    """Latest send date >= earliest whose settlement lands on or before target."""
    d = target
    while d >= earliest:
        if (_always_on(rail, cfg) or is_business_day(d)) and settle_date_for(rail, cfg, d) <= target:
            return d
        d -= timedelta(days=1)
    return None


# --- terms & discount ------------------------------------------------------------------


def parse_terms(terms: str | None) -> Terms:
    if not terms:
        return Terms()
    m = TERMS_RE.search(terms)
    if m:
        return Terms(discount_pct=float(m["pct"]), discount_days=int(m["disc"]), net_days=int(m["net"]))
    m = NET_RE.search(terms)
    return Terms(net_days=int(m["net"])) if m else Terms()


def discount_annualized_bps(t: Terms) -> float:
    """Implied annual return of paying early: pct/(100-pct) * 365/(net - discount days)."""
    if not t.discount_pct or not t.net_days or t.net_days <= t.discount_days:
        return 0.0
    return t.discount_pct / (100 - t.discount_pct) * 365 / (t.net_days - t.discount_days) * 10_000


# --- planning ----------------------------------------------------------------------------


def plan(*, amount_cents: int, vendor_rails: list[str], card_accepting: bool, terms: str | None,
         invoice_date: date | None, due_date: date | None, risk_score: int, now: datetime) -> RailPlan:
    cfg = rails_config()
    rails = cfg["rails"]
    today = now.date()
    reasons: list[str] = []

    # 2. target date (discount deadline vs due date)
    t = parse_terms(terms)
    due = due_date or (invoice_date + timedelta(days=t.net_days) if invoice_date and t.net_days else today)
    target, discount = due, 0
    if t.discount_pct and invoice_date:
        deadline = invoice_date + timedelta(days=t.discount_days)
        annual = discount_annualized_bps(t)
        hurdle = cfg["cost_of_capital_annual_bps"]
        if deadline >= today and annual > hurdle:
            target, discount = deadline, int(amount_cents * t.discount_pct / 100)
            reasons.append(f"early-pay discount {t.discount_pct:g}% is worth {annual / 100:.1f}%/yr > cost of "
                           f"capital {hurdle / 100:.1f}%: settle by {deadline}")
        elif deadline < today:
            reasons.append(f"discount window closed on {deadline}; paying to the due date")
        else:
            reasons.append(f"discount worth {annual / 100:.1f}%/yr does not beat cost of capital {hurdle / 100:.1f}%")
    urgent = target <= today
    if urgent:
        reasons.append(f"urgent: must settle by {target} (today is {today})")
    pay_amount = amount_cents - discount

    # 1. eligibility
    options: list[RailOption] = []
    for name, rc in rails.items():
        fee = rc["fee_cents"]
        rebate = pay_amount * rc.get("rebate_bps", 0) // 10_000
        if name not in vendor_rails:
            options.append(RailOption(rail=name, eligible=False, reason="vendor not enrolled"))
            continue
        if rc.get("requires") == "card_accepting" and not card_accepting:
            options.append(RailOption(rail=name, eligible=False, reason="vendor does not accept cards"))
            continue
        if rc.get("max_cents") is not None and pay_amount > rc["max_cents"]:
            options.append(RailOption(rail=name, eligible=False, reason=f"amount over rail limit {rc['max_cents']}"))
            continue
        if name in INSTANT_RAILS:
            if not urgent:
                options.append(RailOption(rail=name, eligible=False, reason="instant rails only for urgent payments"))
                continue
            if risk_score > cfg["instant_max_risk_score"]:
                options.append(RailOption(rail=name, eligible=False,
                                          reason=f"risk {risk_score} too high for an irrevocable rail"))
                continue
        earliest = earliest_send(name, rc, now)
        send = latest_send(name, rc, earliest, target)
        meets = send is not None
        if not meets:
            send = earliest
        options.append(RailOption(rail=name, eligible=True, reason="eligible", send_date=send,
                                  settle_date=settle_date_for(name, rc, send), fee_cents=fee,
                                  rebate_cents=rebate, meets_target=meets))

    # 3. cheapest rail that meets the date; otherwise the fastest eligible rail (late)
    eligible = [o for o in options if o.eligible]
    on_time = [o for o in eligible if o.meets_target]
    if on_time:
        best = min(on_time, key=lambda o: (o.net_cost_cents, o.rail))
        reasons.append(f"{best.rail} is the cheapest rail that settles by {target} "
                       f"(net cost {best.net_cost_cents} cents)")
    elif eligible:
        best = min(eligible, key=lambda o: (o.settle_date, o.net_cost_cents, o.rail))
        reasons.append(f"no eligible rail settles by {target}; {best.rail} is fastest (settles {best.settle_date})")
    else:
        return RailPlan(rail=None, send_date=None, settle_date=None, target_date=target, urgent=urgent,
                        on_time=False, pay_amount_cents=pay_amount, options=options,
                        reasons=reasons + ["no eligible rail for this vendor"])

    if discount and not best.meets_target:  # missed the window: pay full amount
        discount, pay_amount = 0, amount_cents
        reasons.append("cannot settle inside the discount window; paying the full amount")
    return RailPlan(rail=best.rail, send_date=best.send_date, settle_date=best.settle_date, target_date=target,
                    urgent=urgent, on_time=best.meets_target, fee_cents=best.fee_cents,
                    rebate_cents=pay_amount * rails[best.rail].get("rebate_bps", 0) // 10_000,
                    discount_cents=discount, pay_amount_cents=pay_amount, reasons=reasons, options=options)


def demo_now(run_date: date, run_time: time) -> datetime:
    return datetime.combine(run_date, run_time)
