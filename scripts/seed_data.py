"""Reset the DB and seed the demo: a 12-property portfolio, ~30 vendors, 6 months of
payment history, and 25 invoices for Monday's AP run covering the 9 demo cases (SPEC §8).

Outputs (under data/):
  proppay.db                 SQLite (via DB_PATH)
  invoices/*.pdf             Monday's 25 invoices, 4 layouts
  emails/*.eml               vendor emails (with the invoice PDF attached)
  ofac/sdn.csv               fictional SDN sample list
  demo_manifest.jsonl        per-invoice ground truth + expected outcome (evals/tests only)
  mock_bank_scenarios.json   seeded mock-bank outcomes (e.g. the R03 return)

Deterministic: fixed seeds, fixed run date (DEMO_RUN_DATE).
    uv run python scripts/seed_data.py
"""

from __future__ import annotations

import json
import random
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from faker import Faker  # noqa: E402
from sqlalchemy import Engine  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import audit  # noqa: E402
from app.config import get_settings, policy_config, rails_config  # noqa: E402
from app.db import get_engine, init_db  # noqa: E402
from app.models import (  # noqa: E402
    AccountType,
    Invoice,
    InvoiceStatus,
    OfacStatus,
    Payment,
    PaymentBatch,
    PaymentStatus,
    POStatus,
    Property,
    PurchaseOrder,
    Rail,
    TinMatchStatus,
    Vendor,
    VendorBankAccount,
    WorkOrder,
    WorkOrderStatus,
)
from app.payments import vault  # noqa: E402
from app.payments.routing import aba_check_digit  # noqa: E402
from scripts.make_invoice_pdfs import InvoiceDoc, LineItem, Remit, render  # noqa: E402

SEED = 42
OPERATOR = "Bluestem Residential LLC"
AP_EMAIL = "ap@bluestemresidential.com"
TX_CITIES = ["Austin", "Round Rock", "San Marcos", "Georgetown", "Pflugerville", "Kyle"]
BANKS = ["First Prairie Bank", "Lone Star Community Bank", "Gulf Coast Federal Credit Union",
         "Hill Country National Bank", "Brazos Valley Bank", "Trinity Commerce Bank"]

ACH_DEFAULT = ["ach_standard", "ach_same_day", "check"]


# --- vendor master -----------------------------------------------------------


@dataclass
class VSpec:
    key: str
    name: str
    category: str
    domain: str
    template: str
    mean: int  # typical invoice, cents
    history: int  # settled payments in the last 6 months
    prefix: str
    rails: list[str] = field(default_factory=lambda: list(ACH_DEFAULT))
    card: bool = False
    check_only: bool = False
    onboarded_days_ago: int = 600


VENDORS: list[VSpec] = [
    VSpec("acme", "Acme Plumbing LLC", "plumbing", "acmeplumbing.com", "classic", 260_000, 6, "AP-"),
    VSpec("greenleaf", "Greenleaf Landscaping Inc.", "landscaping", "greenleaflandscape.com", "compact", 197_500, 6, "INV-"),
    VSpec("polar", "Polar Air HVAC Services", "hvac", "polarairhvac.com", "modern", 310_000, 5, "PA-",
          rails=["ach_standard", "ach_same_day", "rtp", "check"]),  # receiving bank is on RTP, not FedNow
    VSpec("summit", "Summit Builders Group", "general_contractor", "summitbuilders.com", "pay_app", 46_000_000, 5, "SBG-PA-",
          rails=["wire"]),  # construction draws are paid by wire per the contract
    VSpec("brightclean", "BrightClean Janitorial", "cleaning", "brightcleantx.com", "modern", 180_000, 6, "BC-",
          rails=[*ACH_DEFAULT, "virtual_card"], card=True),
    VSpec("lonestar", "Lone Star Maintenance Supply", "supplies", "lonestarmro.com", "compact", 95_000, 7, "LS",
          rails=[*ACH_DEFAULT, "virtual_card"], card=True),
    VSpec("sentinel", "Sentinel Patrol Security", "security", "sentinelpatrol.com", "classic", 420_000, 6, "SPS-"),
    VSpec("lakeside", "Lakeside Pest Control", "pest_control", "lakesidepest.com", "modern", 264_000, 6, "LPC-"),
    VSpec("capital", "Capital Electric Co.", "electrical", "capitalelectricco.com", "classic", 185_000, 5, "CE"),
    VSpec("hilltop", "Hilltop Roofing & Gutters", "roofing", "hilltoproofing.com", "compact", 340_000, 4, "HR-"),
    VSpec("freshcoat", "Fresh Coat Painting", "painting", "freshcoatpaint.com", "modern", 290_000, 5, "FCP-"),
    VSpec("vertex", "Vertex Elevator Service", "elevator", "vertexelevator.com", "classic", 145_000, 6, "VES-"),
    VSpec("bluewave", "BlueWave Pool Care", "pool", "bluewavepools.com", "compact", 88_000, 6, "BW"),
    VSpec("metrowaste", "Metro Waste Haulers", "waste", "metrowastehaulers.com", "classic", 210_000, 6, "MWH-"),
    VSpec("keystone", "Keystone Lock & Key", "locksmith", "keystonelock.com", "compact", 64_000, 5, "KLK-"),
    VSpec("clearview", "ClearView Window Cleaning", "cleaning", "clearviewwindows.com", "modern", 120_000, 5, "CV-"),
    VSpec("oakridge", "Oakridge Appliance Repair", "appliance", "oakridgeappliance.com", "classic", 72_000, 6, "OAR-"),
    VSpec("prairie", "Prairie Flooring & Carpet", "flooring", "prairieflooring.com", "modern", 380_000, 4, "PFC-"),
    VSpec("riverbend", "Riverbend Fire Protection", "fire_safety", "riverbendfire.com", "classic", 156_000, 5, "RFP-"),
    VSpec("bluebonnet", "Bluebonnet Irrigation", "landscaping", "bluebonnetirrigation.com", "compact", 98_000, 6, "BBI-"),
    VSpec("northgate", "Northgate Mechanical", "hvac", "northgatemech.com", "modern", 230_000, 5, "NGM-"),
    VSpec("cedar", "Cedar Junk Removal", "waste", "cedarjunk.com", "compact", 55_000, 5, "CJR-"),
    VSpec("signal", "Signal Signage Co.", "signage", "signalsignage.com", "classic", 135_000, 4, "SIG-"),
    VSpec("apex", "Apex Parking Lot Services", "paving", "apexparkinglots.com", "modern", 410_000, 4, "APX-"),
    VSpec("harbor", "Harbor Glass & Mirror", "glass", "harborglass.com", "classic", 110_000, 4, "HGM-"),
    VSpec("evergreen", "Evergreen Tree Care", "landscaping", "evergreentreecare.com", "compact", 175_000, 4, "ETC-"),
    VSpec("lumen", "Lumen Gas & Power", "utilities", "lumengaspower.com", "classic", 820_000, 6, "LGP-"),
    VSpec("paloma", "Paloma Water District", "utilities", "palomawater.org", "classic", 460_000, 6, "PWD-",
          rails=["check"], check_only=True),
    VSpec("quickfix", "QuickFix Handyman Services", "handyman", "quickfixhandyman.com", "modern", 45_000, 1, "QF-",
          onboarded_days_ago=25),
    VSpec("meridian", "Meridian Supply & Logistics", "supplies", "meridiansupplylogistics.com", "modern", 295_000, 0, "MSL-",
          onboarded_days_ago=7),
]
V = {v.key: v for v in VENDORS}

ITEMS: dict[str, list[str]] = {
    "plumbing": ["Water heater replacement", "Drain line hydro-jetting", "Emergency leak repair", "Fixture replacement labor", "Shut-off valve replacement"],
    "landscaping": ["Monthly grounds maintenance", "Seasonal mulch install", "Shrub trimming", "Irrigation zone repair", "Tree limb removal"],
    "hvac": ["Compressor replacement", "Refrigerant recharge (R-410A)", "Emergency after-hours call-out", "Condenser fan motor", "Preventive maintenance visit"],
    "general_contractor": [],
    "cleaning": ["Common area janitorial", "Move-out unit clean", "Carpet extraction", "Window washing - exterior", "Lobby floor buff"],
    "supplies": ["HVAC filters 20x25x1 (case)", "LED A19 bulbs (case)", "Janitorial chemicals", "Smoke detector batteries", "Paint supplies"],
    "security": ["Night patrol services", "Gate access monitoring", "Event security staffing", "Camera system service"],
    "pest_control": ["Quarterly pest treatment", "Termite inspection", "Rodent exclusion", "Bed bug heat treatment"],
    "electrical": ["Panel inspection", "Parking lot light repair", "Outlet/GFCI replacement", "Exterior lighting retrofit"],
    "roofing": ["Roof leak repair", "Gutter cleaning", "Shingle replacement", "Flashing reseal"],
    "painting": ["Unit interior repaint", "Exterior trim painting", "Drywall patch and paint", "Stairwell repaint"],
    "elevator": ["Monthly elevator maintenance", "State inspection support", "Door operator adjustment"],
    "pool": ["Weekly pool service", "Chemical balancing", "Pump seal replacement", "Filter cartridge replacement"],
    "waste": ["Dumpster service - 8yd", "Bulk item haul-off", "Recycling pickup", "Compactor service"],
    "locksmith": ["Unit rekey", "Master key system update", "Lockout service", "Mailbox lock replacement"],
    "appliance": ["Refrigerator repair", "Dishwasher pump replacement", "Washer repair", "Range igniter replacement"],
    "flooring": ["LVP install", "Carpet replacement", "Tile repair", "Floor prep and leveling"],
    "fire_safety": ["Annual sprinkler inspection", "Fire extinguisher service", "Alarm panel test", "Backflow test"],
    "signage": ["Leasing banner", "Monument sign repair", "Unit number plaques"],
    "paving": ["Parking lot restripe", "Pothole repair", "Sealcoat", "Curb paint"],
    "glass": ["Patio door glass replacement", "Window pane replacement", "Mirror install"],
    "utilities": ["Electric service", "Gas service", "Water service", "Sewer service"],
    "handyman": ["General repairs", "Fence repair", "Door adjustment"],
}

PROPERTY_NAMES = ["Maple Court Apartments", "Riverstone Flats", "Cedar Ridge Townhomes", "Bluebonnet Commons",
                  "Lakeview Terrace", "Oak Hollow Residences", "Pecan Grove Lofts", "Sunset Pointe",
                  "Willow Creek Villas", "Granite Park Apartments", "Mesquite Station", "Hill Country Lofts"]

# Fictional SDN-format sample (no real entries). Real list: https://ofac.treasury.gov/sdn-list
SDN_SAMPLE = [
    ("90001", "MERIDIAN SUPPLY AND LOGISTICS FZE", "Entity", "SAMPLE-1", "Fictional sample entry for demo"),
    ("90002", "NORTHWIND MARITIME HOLDINGS LTD", "Entity", "SAMPLE-1", "Fictional sample entry for demo"),
    ("90003", "CASPIAN RIDGE TRADING CO", "Entity", "SAMPLE-2", "Fictional sample entry for demo"),
    ("90004", "ORION BLUE FREIGHT SA", "Entity", "SAMPLE-2", "Fictional sample entry for demo"),
    ("90005", "GOLDEN ANCHOR SHIPPING LLC", "Entity", "SAMPLE-3", "Fictional sample entry for demo"),
    ("90006", "SILVERLINE PETROCHEM FZCO", "Entity", "SAMPLE-3", "Fictional sample entry for demo"),
    ("90007", "TALON INDUSTRIAL EXPORTS", "Entity", "SAMPLE-1", "Fictional sample entry for demo"),
    ("90008", "BRIGHTWATER COMMODITIES GMBH", "Entity", "SAMPLE-2", "Fictional sample entry for demo"),
]


# --- helpers -----------------------------------------------------------------


def routing_number(rng: random.Random) -> str:
    first8 = f"{rng.randint(1, 12):02d}" + "".join(str(rng.randint(0, 9)) for _ in range(6))
    return first8 + aba_check_digit(first8)


def account_number(rng: random.Random) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(rng.choice([10, 11, 12])))


def split_items(rng: random.Random, category: str, total: int) -> list[LineItem]:
    """1-3 line items that sum exactly to `total` cents."""
    names = rng.sample(ITEMS[category], k=min(len(ITEMS[category]), rng.randint(1, 3)))
    if len(names) == 1:
        return [LineItem(names[0], 1, total, total)]
    weights = [rng.uniform(0.5, 1.5) for _ in names]
    items, remaining = [], total
    for name, w in zip(names[:-1], weights[:-1]):
        part = int(total * w / sum(weights))
        qty = rng.choice([1, 1, 2, 4])
        unit = max(1, part // qty)
        items.append(LineItem(name, qty, unit, unit * qty))
        remaining -= unit * qty
    items.append(LineItem(names[-1], 1, remaining, remaining))
    return items


def near(rng: random.Random, mean: int, rel_sd: float = 0.12) -> int:
    """Amount near the vendor's mean, rounded to whole dollars."""
    return max(5_000, round(rng.gauss(mean, mean * rel_sd) / 100) * 100)


def fee_for(rail: str, amount: int) -> tuple[int, int]:
    cfg = rails_config()["rails"][rail]
    rebate = amount * cfg.get("rebate_bps", 0) // 10_000
    return cfg["fee_cents"], rebate


def money_str(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}" if cents >= 0 else "-" + money_str(-cents)


@dataclass
class Ctx:
    session: Session
    rng: random.Random
    fake: Faker
    run_date: date
    out_dir: Path
    vendors: dict[str, Vendor] = field(default_factory=dict)
    accounts: dict[str, tuple[VendorBankAccount, str, str]] = field(default_factory=dict)  # key -> (row, bank, full acct)
    props: list[Property] = field(default_factory=list)
    contact: dict[str, dict] = field(default_factory=dict)
    seq: dict[str, int] = field(default_factory=dict)
    po_seq: int = 4100

    def next_po(self) -> str:
        self.po_seq += 1
        return f"PO-{self.po_seq}"

    def next_inv(self, key: str) -> str:
        self.seq[key] += 1
        return f"{V[key].prefix}{self.seq[key]}"

    def bill_to(self, prop: Property) -> list[str]:
        return [OPERATOR, prop.name, prop.address]


# --- builders ----------------------------------------------------------------


def seed_properties(ctx: Ctx) -> None:
    for name in PROPERTY_NAMES:
        addr = f"{ctx.fake.street_address()}, {ctx.rng.choice(TX_CITIES)}, TX {ctx.fake.zipcode_in_state('TX')}"
        p = Property(name=name, address=addr, units=ctx.rng.randrange(48, 324, 4))
        ctx.session.add(p)
        ctx.props.append(p)
    ctx.session.flush()


def seed_vendors(ctx: Ctx) -> None:
    for spec in VENDORS:
        created = datetime.combine(ctx.run_date - timedelta(days=spec.onboarded_days_ago), time(10), timezone.utc)
        new = spec.key == "meridian"
        v = Vendor(
            legal_name=spec.name,
            email_domain=spec.domain,
            tin_masked=f"XX-XXX{ctx.rng.randint(1000, 9999)}",
            w9_received=True,
            tin_match_status=TinMatchStatus.pending if new else TinMatchStatus.matched,
            ofac_status=OfacStatus.pending if new else OfacStatus.clear,
            category=spec.category,
            card_accepting=spec.card,
            enrolled_rails=spec.rails,
            created_at=created,
        )
        ctx.session.add(v)
        ctx.session.flush()
        ctx.vendors[spec.key] = v
        ctx.seq[spec.key] = ctx.rng.randint(1000, 7000)
        ctx.contact[spec.key] = {
            "address": [ctx.fake.street_address(), f"{ctx.rng.choice(TX_CITIES)}, TX {ctx.fake.zipcode_in_state('TX')}"],
            "phone": ctx.fake.numerify("(512) ###-####"),
            "email": f"billing@{spec.domain}",
        }
        if spec.check_only:
            continue
        acct = account_number(ctx.rng)
        bank = ctx.rng.choice(BANKS)
        row = VendorBankAccount(
            vendor_id=v.id,
            routing=routing_number(ctx.rng),
            account_last4=acct[-4:],
            account_hash=vault.tokenize(ctx.session, acct),
            account_type=AccountType.checking,
            validated=not new,
            effective_from=(created + timedelta(days=1)).date(),
        )
        ctx.session.add(row)
        ctx.accounts[spec.key] = (row, bank, acct)
    # Greenleaf's history must end at INV-1043 so the resubmission is INV-1043-A.
    ctx.seq["greenleaf"] = 1043 - V["greenleaf"].history
    ctx.session.flush()


def _history_rail(spec: VSpec) -> str:
    if spec.check_only:
        return "check"
    if spec.card:
        return "virtual_card"
    if spec.key == "summit":
        return "wire"
    return "ach_standard"


def seed_history(ctx: Ctx) -> dict:
    """Six months of paid invoices + settled payments. Returns facts later cases depend on."""
    s, rng = ctx.session, ctx.rng
    start = ctx.run_date - timedelta(days=182)
    facts: dict = {"summit_draws": []}
    ach_by_date: dict[date, list[Payment]] = {}

    for spec in VENDORS:
        n = spec.history
        if n == 0:
            continue
        span = (ctx.run_date - timedelta(days=10)) - start
        if spec.onboarded_days_ago < 182:
            start_v = ctx.run_date - timedelta(days=spec.onboarded_days_ago - 2)
            span = (ctx.run_date - timedelta(days=5)) - start_v
        else:
            start_v = start
        for i in range(n):
            last = i == n - 1
            inv_date = start_v + timedelta(days=int(span.days * (i + 0.5) / n) + rng.randint(-3, 3))
            if spec.key == "greenleaf" and last:
                inv_date = ctx.run_date - timedelta(days=17)  # INV-1043, paid ~10 days ago
            amount = near(rng, spec.mean, 0.08 if spec.key == "summit" else 0.12)
            prop = rng.choice(ctx.props)
            po = PurchaseOrder(po_number=ctx.next_po(), vendor_id=ctx.vendors[spec.key].id,
                               property_id=prop.id, amount_cents=amount, status=POStatus.closed)
            s.add(po)
            s.flush()
            s.add(WorkOrder(po_id=po.id, status=WorkOrderStatus.completed,
                            completed_at=datetime.combine(inv_date - timedelta(days=2), time(15), timezone.utc)))
            number = ctx.next_inv(spec.key)
            inv = Invoice(
                vendor_id=ctx.vendors[spec.key].id, invoice_number=number, invoice_date=inv_date,
                due_date=inv_date + timedelta(days=30), amount_cents=amount, terms="Net 30",
                po_number=po.po_number, source_file=f"history/{number}.pdf", status=InvoiceStatus.paid,
                risk_score=0,
            )
            s.add(inv)
            s.flush()
            rail = _history_rail(spec)
            fee, rebate = fee_for(rail, amount)
            pay_date = min(inv_date + timedelta(days=rng.randint(18, 28)), ctx.run_date - timedelta(days=3))
            if spec.key == "greenleaf" and last:
                pay_date = inv_date + timedelta(days=7)
                facts["greenleaf_last"] = {"invoice_number": number, "amount": amount, "date": inv_date,
                                           "po_number": po.po_number, "property": prop}
            pay = Payment(invoice_id=inv.id, rail=Rail(rail), amount_cents=amount, fee_cents=fee,
                          rebate_cents=rebate, scheduled_date=pay_date, status=PaymentStatus.settled,
                          idempotency_key=f"pending-{inv.id}")
            s.add(pay)
            s.flush()
            pay.idempotency_key = f"{pay.id}-{pay.attempt}"
            if rail.startswith("ach"):
                ach_by_date.setdefault(pay_date, []).append(pay)
            if spec.key == "summit":
                facts["summit_draws"].append(amount)

    for d, pays in sorted(ach_by_date.items()):
        batch = PaymentBatch(rail=Rail.ach_standard, file_path=None, total_cents=sum(p.amount_cents for p in pays),
                             entry_count=len(pays),
                             created_at=datetime.combine(d - timedelta(days=1), time(16), timezone.utc))
        s.add(batch)
        s.flush()
        for p in pays:
            p.batch_id = batch.id
    s.flush()
    return facts


def set_trusted(ctx: Ctx) -> None:
    need = policy_config()["vendor_trust"]["min_settled_payments"]
    for v in ctx.vendors.values():
        settled = ctx.session.exec(
            select(Payment).join(Invoice).where(Invoice.vendor_id == v.id, Payment.status == PaymentStatus.settled)
        ).all()
        v.trusted = len(settled) >= need
    ctx.session.flush()


# --- Monday's AP run ---------------------------------------------------------


@dataclass
class Case:
    key: str
    demo_case: int
    doc: InvoiceDoc
    expected: dict
    channel: str = "upload"  # upload | email
    email: dict | None = None
    is_fraud: bool = False
    po_amount: int | None = None  # None: no PO in the system
    po_status: POStatus = POStatus.open
    wo_completed: datetime | None = None
    property: Property | None = None
    bank_change_text: bool = False


def _remit(ctx: Ctx, key: str) -> Remit:
    row, bank, _ = ctx.accounts[key]
    return Remit(bank, row.routing, row.account_last4)


def _doc(ctx: Ctx, key: str, prop: Property, number: str, inv_date: date, due: date | None, terms: str | None,
         po: str | None, items: list[LineItem], remit: Remit | None = None, notes: list[str] | None = None,
         pay_app: dict | None = None) -> InvoiceDoc:
    c = ctx.contact[key]
    return InvoiceDoc(template=V[key].template, vendor_name=V[key].name, vendor_address=c["address"],
                      vendor_phone=c["phone"], vendor_email=c["email"], bill_to=ctx.bill_to(prop),
                      invoice_number=number, invoice_date=inv_date, due_date=due, terms=terms, po_number=po,
                      line_items=items, remit=remit, notes=notes or [], pay_app=pay_app)


ROUTINE = ["sentinel", "lakeside", "capital", "hilltop", "freshcoat", "vertex", "bluewave", "metrowaste", "keystone",
           "clearview", "oakridge", "prairie", "riverbend", "bluebonnet", "northgate", "cedar", "signal", "apex"]
AUTO_ACH = {"decision": "AUTO_APPROVE", "rail": "ach_standard", "signals": []}


def build_cases(ctx: Ctx, facts: dict) -> list[Case]:
    rng, rd = ctx.rng, ctx.run_date
    cases: list[Case] = []

    # 1 (+7, +8): 18 routine invoices from trusted vendors, < $5k, full 3-way match.
    for i, key in enumerate(ROUTINE):
        prop = rng.choice(ctx.props)
        inv_date = rd - timedelta(days=rng.randint(2, 12))
        amount = min(near(rng, V[key].mean, 0.06), 490_000)
        terms, due = "Net 30", inv_date + timedelta(days=30)
        demo_case, expected, notes = 1, dict(AUTO_ACH), []
        if key == "lakeside":  # case 8: early-pay discount
            inv_date, terms = rd - timedelta(days=4), "2/10 Net 30"
            due = inv_date + timedelta(days=30)
            demo_case = 8
            expected = {**AUTO_ACH, "pay_early": True,
                        "discount_cents": amount * 2 // 100, "discount_deadline": (inv_date + timedelta(days=10)).isoformat()}
            notes = ["2% discount if paid within 10 days of invoice date."]
        if key == "keystone":  # case 7: ACH that comes back R03
            demo_case = 7
            expected = {**AUTO_ACH, "return_code": "R03", "post_return": "invoice reopened, vendor flagged, notice drafted"}
        po = ctx.next_po()
        doc = _doc(ctx, key, prop, ctx.next_inv(key), inv_date, due, terms, po, split_items(rng, V[key].category, amount),
                   remit=_remit(ctx, key) if (i % 2 == 0 or key == "keystone") else None, notes=notes)
        channel, email = "upload", None
        if key in ("sentinel", "bluebonnet"):
            channel = "email"
            email = {"from": f"{V[key].name} Billing <billing@{V[key].domain}>",
                     "subject": f"Invoice {doc.invoice_number} - {prop.name}",
                     "body": (f"Hello,\n\nPlease find attached invoice {doc.invoice_number} for services at "
                              f"{prop.name}.\n\nThank you,\nAccounts Receivable\n{V[key].name}\n")}
        # PO within the 2% tolerance: exact, or up to 1.5% above the invoice
        po_amount = amount if i % 3 else amount + amount * rng.randint(0, 15) // 1000
        cases.append(Case(key, demo_case, doc, expected, channel=channel, email=email, po_amount=po_amount,
                          wo_completed=datetime.combine(inv_date - timedelta(days=1), time(14), timezone.utc),
                          property=prop))

    # 2: BEC — look-alike domain asks to change bank details.
    prop = rng.choice(ctx.props)
    new_acct, new_routing = account_number(rng), routing_number(rng)
    inv_date, amount = rd - timedelta(days=4), 284_000
    doc = _doc(ctx, "acme", prop, ctx.next_inv("acme"), inv_date, inv_date + timedelta(days=7), "Net 7", ctx.next_po(),
               split_items(rng, "plumbing", amount),
               remit=Remit("Pacific Crest Bank", new_routing, new_acct[-4:], account_full=new_acct),
               notes=["IMPORTANT: Our banking details have changed. Please update your records and remit",
                      "all payments to the new account shown below effective immediately."])
    cases.append(Case(
        "acme", 2, doc, is_fraud=True, channel="email", bank_change_text=True, po_amount=amount, property=prop,
        wo_completed=datetime.combine(inv_date - timedelta(days=2), time(11), timezone.utc),
        email={"from": "Acme Plumbing Accounts <accounts@acmeplumbimg.com>",
               "subject": f"URGENT: Updated remittance details - Invoice {doc.invoice_number}",
               "body": ("Hi Accounts Payable team,\n\n"
                        "Please be advised that Acme Plumbing LLC has changed banks. Kindly update our vendor "
                        "record with the details below and use them for the attached invoice and all future "
                        "payments:\n\n"
                        f"  Bank: Pacific Crest Bank\n  Routing number: {new_routing}\n  Account number: {new_acct}\n\n"
                        "Our previous account is being closed this week, so payments sent there will be returned. "
                        "We need this invoice paid today - our crews cannot be dispatched to your properties "
                        "until the account is current.\n\n"
                        "Please confirm by reply email only; our phone lines are down during the transition.\n\n"
                        "Thank you,\nDana Whitfield\nAccounts Receivable\nAcme Plumbing LLC\n")},
        expected={"decision": "HOLD_CALLBACK", "rail": None,
                  "signals": ["BANK_DETAILS_CHANGED", "BANK_CHANGE_VIA_EMAIL", "LOOKALIKE_VENDOR"],
                  "urgency": "required",
                  "new_bank": {"routing": new_routing, "account_last4": new_acct[-4:]}},
    ))

    # 3: duplicate — Greenleaf INV-1043 (already paid) resubmitted as INV-1043-A.
    g = facts["greenleaf_last"]
    rng_g = random.Random(SEED + 3)  # same line items as the original
    doc = _doc(ctx, "greenleaf", g["property"], g["invoice_number"] + "-A", g["date"], g["date"] + timedelta(days=30),
               "Net 30", g["po_number"], split_items(rng_g, "landscaping", g["amount"]))
    cases.append(Case("greenleaf", 3, doc, po_amount=None, property=g["property"],
                      expected={"decision": "SINGLE_APPROVAL", "rail": None, "signals": ["DUPLICATE_INVOICE"],
                                "duplicate_of": g["invoice_number"]}))

    # 4: emergency HVAC, $3,800, due today, clean history -> RTP.
    prop = rng.choice(ctx.props)
    items = [LineItem("Emergency after-hours call-out", 1, 45_000, 45_000),
             LineItem("Compressor replacement", 1, 335_000, 335_000)]
    doc = _doc(ctx, "polar", prop, ctx.next_inv("polar"), rd, rd, "Due upon receipt", ctx.next_po(), items,
               remit=_remit(ctx, "polar"),
               notes=["EMERGENCY SERVICE: no cooling at building B, repaired overnight. Payment due on receipt."])
    cases.append(Case("polar", 4, doc, po_amount=380_000, property=prop,
                      wo_completed=datetime.combine(rd, time(6, 30), timezone.utc),
                      expected={"decision": "AUTO_APPROVE", "rail": "rtp", "signals": [],
                                "urgency": "either"}))  # emergency wording; decision is the same either way

    # 5: GC construction draw, $480,000 -> dual approval -> wire.
    prop = ctx.props[0]
    prev = sum(facts["summit_draws"])
    gross, net = 53_333_333, 48_000_000  # 10% retainage: 533,333.33 - 53,333.33 = 480,000.00
    completed_to_date = (prev * 10 // 9) + gross
    sov = [("Framing - Building C", 18_250_000), ("MEP rough-in - Building C", 16_400_000),
           ("Roofing - Building C", 9_800_000)]
    items = [LineItem(d, 1, a, a) for d, a in sov]
    items.append(LineItem("Site work and utilities", 1, gross - sum(a for _, a in sov), gross - sum(a for _, a in sov)))
    items.append(LineItem("Less retainage (10%)", 1, -(gross - net), -(gross - net)))
    app_no = V["summit"].history + 1
    doc = _doc(ctx, "summit", prop, f"{V['summit'].prefix}{app_no:03d}", rd - timedelta(days=3), rd + timedelta(days=2),
               "Net 5 per contract", ctx.next_po(), items, remit=_remit(ctx, "summit"),
               pay_app={"application_no": app_no, "project": f"{prop.name} - Phase II (Buildings C & D)",
                        "original_contract_cents": 420_000_000, "change_orders_cents": 8_650_000,
                        "completed_to_date_cents": completed_to_date,
                        "retainage_to_date_cents": completed_to_date // 10,
                        "previous_certificates_cents": prev})
    cases.append(Case("summit", 5, doc, po_amount=net, property=prop,
                      wo_completed=datetime.combine(rd - timedelta(days=4), time(17), timezone.utc),
                      expected={"decision": "DUAL_APPROVAL", "rail": "wire", "signals": []}))

    # 6: two card-accepting vendors -> virtual card, rebate recorded.
    for key in ("brightclean", "lonestar"):
        prop = rng.choice(ctx.props)
        inv_date = rd - timedelta(days=rng.randint(3, 9))
        amount = near(rng, V[key].mean, 0.06)
        doc = _doc(ctx, key, prop, ctx.next_inv(key), inv_date, inv_date + timedelta(days=30), "Net 30", ctx.next_po(),
                   split_items(rng, V[key].category, amount),
                   notes=["We accept Visa, Mastercard and ACH."])
        rebate = amount * rails_config()["rails"]["virtual_card"]["rebate_bps"] // 10_000
        cases.append(Case(key, 6, doc, po_amount=amount, property=prop,
                          wo_completed=datetime.combine(inv_date - timedelta(days=1), time(12), timezone.utc),
                          expected={"decision": "AUTO_APPROVE", "rail": "virtual_card", "signals": [],
                                    "rebate_cents": rebate}))

    # 9: vendor name close to an SDN entry -> OFAC hard block.
    prop = rng.choice(ctx.props)
    inv_date = rd - timedelta(days=2)
    doc = _doc(ctx, "meridian", prop, ctx.next_inv("meridian"), inv_date, inv_date + timedelta(days=30), "Net 30", None,
               split_items(rng, "supplies", 295_000), remit=_remit(ctx, "meridian"))
    cases.append(Case("meridian", 9, doc, po_amount=None, property=prop,
                      expected={"decision": "BLOCK", "rail": None,
                                "signals": ["OFAC_HIT", "FIRST_TIME_PAYEE", "NEW_ACCOUNT_RECENT"],
                                "sdn_match": "MERIDIAN SUPPLY AND LOGISTICS FZE"}))
    return cases


def truth(case: Case) -> dict:
    """Extraction ground truth, shaped like app.schemas.InvoiceExtraction (minus confidence)."""
    d = case.doc
    return {
        "vendor_name": d.vendor_name,
        "invoice_number": d.invoice_number,
        "invoice_date": d.invoice_date.isoformat(),
        "due_date": d.due_date.isoformat() if d.due_date else None,
        "amount": money_str(d.total_cents),
        "currency": "USD",
        "terms": d.terms,
        "po_number": d.po_number,
        "line_items": [{"description": li.description, "quantity": li.quantity,
                        "unit_price": money_str(li.unit_cents), "amount": money_str(li.amount_cents)}
                       for li in d.line_items],
        "remit_to_bank": {"routing": d.remit.routing, "account_last4": d.remit.account_last4} if d.remit else None,
        "email_mentions_bank_change": case.bank_change_text,
    }


def write_email(case: Case, pdf: Path, path: Path, run_date: date) -> None:
    e = case.email
    msg = EmailMessage()
    msg["From"] = e["from"]
    msg["To"] = AP_EMAIL
    msg["Subject"] = e["subject"]
    msg["Date"] = format_datetime(datetime.combine(run_date - timedelta(days=1), time(19, 42), timezone.utc))
    msg["Message-ID"] = f"<{case.doc.invoice_number}.{run_date:%Y%m%d}@{e['from'].split('@')[1].rstrip('>')}>"
    msg.set_content(e["body"])
    msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
    path.write_bytes(bytes(msg))


def seed_monday(ctx: Ctx, facts: dict) -> list[dict]:
    s = ctx.session
    cases = build_cases(ctx, facts)
    manifest = []
    for n, case in enumerate(cases, start=1):
        d = case.doc
        stem = f"{n:02d}_{case.key}_{d.invoice_number}".replace("/", "-")
        pdf = render(d, ctx.out_dir / "invoices" / f"{stem}.pdf")
        email_rel = None
        if case.email:
            email_rel = f"emails/{stem}.eml"
            write_email(case, pdf, ctx.out_dir / email_rel, ctx.run_date)
        if case.po_amount is not None and d.po_number:
            po = PurchaseOrder(po_number=d.po_number, vendor_id=ctx.vendors[case.key].id,
                               property_id=case.property.id, amount_cents=case.po_amount, status=case.po_status)
            s.add(po)
            s.flush()
            s.add(WorkOrder(po_id=po.id, status=WorkOrderStatus.completed, completed_at=case.wo_completed))
        inv = Invoice(source_file=email_rel or f"invoices/{pdf.name}", status=InvoiceStatus.received,
                      is_fraud=case.is_fraud, demo_case=case.demo_case)
        s.add(inv)
        s.flush()
        manifest.append({
            "invoice_id": inv.id, "file": pdf.name, "email_file": email_rel, "channel": case.channel,
            "vendor_key": case.key, "demo_case": case.demo_case, "is_fraud": case.is_fraud,
            "truth": truth(case), "expected": case.expected, "doc": d.to_json(),
        })
    return manifest


def write_side_files(ctx: Ctx, manifest: list[dict]) -> None:
    out = ctx.out_dir
    (out / "ofac").mkdir(parents=True, exist_ok=True)
    lines = ["ent_num,sdn_name,sdn_type,program,remarks"] + [",".join(r) for r in SDN_SAMPLE]
    (out / "ofac" / "sdn.csv").write_text("\n".join(lines) + "\n")

    with (out / "demo_manifest.jsonl").open("w") as f:
        for rec in manifest:
            f.write(json.dumps(rec, sort_keys=True) + "\n")

    row, _, _ = ctx.accounts["keystone"]
    scenarios = {
        "_comment": "Seeded mock-bank outcomes. Keyed by account token (sha256), never the raw number.",
        "ach_returns": [{"account_hash": row.account_hash, "routing": row.routing, "account_last4": row.account_last4,
                         "return_code": "R03", "vendor": V["keystone"].name, "demo_case": 7}],
        "account_validation": {"default": "valid"},
        "tin_match": {"default": "matched"},
    }
    (out / "mock_bank_scenarios.json").write_text(json.dumps(scenarios, indent=2) + "\n")


def clean_outputs(out_dir: Path) -> None:
    (out_dir / "mock_bank_state.json").unlink(missing_ok=True)  # the bank forgets old payments too
    for f in out_dir.glob("checkpoints.db*"):  # paused graph threads belong to the old invoices
        f.unlink()
    for sub in ("invoices", "emails"):
        d = out_dir / sub
        if d.exists():
            for f in d.iterdir():
                if f.suffix in (".pdf", ".eml", ".txt"):
                    f.unlink()
        d.mkdir(parents=True, exist_ok=True)


def seed(engine: Engine, out_dir: Path, run_date: date) -> dict:
    """Reset `engine`'s schema and write all demo data. Returns a summary."""
    init_db(engine, reset=True)
    clean_outputs(out_dir)
    fake = Faker("en_US")
    fake.seed_instance(SEED)
    with Session(engine) as session:
        ctx = Ctx(session=session, rng=random.Random(SEED), fake=fake, run_date=run_date, out_dir=out_dir)
        seed_properties(ctx)
        seed_vendors(ctx)
        facts = seed_history(ctx)
        set_trusted(ctx)
        manifest = seed_monday(ctx, facts)
        write_side_files(ctx, manifest)
        summary = {
            "run_date": run_date.isoformat(),
            "properties": len(ctx.props),
            "vendors": len(ctx.vendors),
            "trusted_vendors": sum(v.trusted for v in ctx.vendors.values()),
            "history_payments": len(session.exec(select(Payment)).all()),
            "monday_invoices": len(manifest),
            "cases": sorted({m["demo_case"] for m in manifest}),
        }
        audit.record(session, actor="seed_script", entity="database", entity_id=None, action="reset_and_seed",
                     after=summary, reason="demo data reset")
        session.commit()
    return summary


def main() -> None:
    settings = get_settings()
    summary = seed(get_engine(), ROOT / "data", settings.demo_run_date)
    try:  # a running mock bank must forget the old run too
        import httpx

        httpx.post(f"{settings.mock_bank_url}/admin/reset", timeout=2)
        summary["mock_bank"] = "reset"
    except Exception:
        summary["mock_bank"] = "not running (state file cleared)"
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
