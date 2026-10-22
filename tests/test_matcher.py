import httpx
import pytest
from sqlmodel import select

from app.agents import matcher
from app.agents.matcher import resolve_vendor, run_match
from app.models import Invoice, InvoiceStatus, PurchaseOrder, Vendor
from tests.fakes import extract_truth


@pytest.fixture(autouse=True)
def _clear_embed_cache():
    matcher._embed_cache.clear()


def no_embedder(texts):
    raise AssertionError("embedding should not be needed when fuzzy is conclusive")


def stub_embedder(texts):
    """Anything mentioning 'polar' points one way, everything else another."""
    return [[1.0, 0.0] if "polar" in t.lower() else [0.0, 1.0] for t in texts]


def match_case(fresh_seed, m, embedder=no_embedder, **overrides):
    s = fresh_seed["session"]
    extract_truth(fresh_seed, m, **overrides)
    return run_match(s, m["invoice_id"], embedder=embedder)


EXPECTED_STATUS = {1: "full", 2: "full", 3: "partial", 4: "full", 5: "full", 6: "full", 7: "full", 8: "full", 9: "none"}


def test_every_demo_case_gets_expected_match_status(fresh_seed):
    for m in fresh_seed["manifest"]:
        r = match_case(fresh_seed, m)
        assert r.match_status == EXPECTED_STATUS[m["demo_case"]], (m["file"], r.reasons)
        assert r.vendor.method == "fuzzy"


def test_full_match_sets_vendor_and_status(fresh_seed):
    s = fresh_seed["session"]
    m = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 4)
    match_case(fresh_seed, m)
    s.commit()
    inv = s.get(Invoice, m["invoice_id"])
    v = s.exec(select(Vendor).where(Vendor.legal_name == m["truth"]["vendor_name"])).one()
    assert inv.vendor_id == v.id and inv.status == InvoiceStatus.matched and inv.match_status == "full"


def test_duplicate_hits_closed_po(fresh_seed):
    m = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 3)
    r = match_case(fresh_seed, m)
    assert r.checks["po_open"] is False and "possible duplicate" in " ".join(r.reasons)


def test_no_po_means_no_match(fresh_seed):
    m = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 9)
    r = match_case(fresh_seed, m)
    assert r.vendor.vendor_id is not None and r.checks["po_referenced"] is False


def test_amount_outside_tolerance_is_partial(fresh_seed):
    m = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 4)
    r = match_case(fresh_seed, m, amount="3990.00", line_items=[])  # +5% vs PO
    assert r.match_status == "partial" and r.checks["amount_within_tolerance"] is False


def test_po_of_another_vendor_is_no_match(fresh_seed):
    s = fresh_seed["session"]
    m = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 4)
    other = next(x for x in fresh_seed["manifest"] if x["demo_case"] == 1)
    r = match_case(fresh_seed, m, po_number=other["truth"]["po_number"])
    assert r.match_status == "none" and r.checks["po_vendor_matches"] is False
    assert s.exec(select(PurchaseOrder).where(PurchaseOrder.po_number == other["truth"]["po_number"])).one()


@pytest.mark.parametrize("name", ["LAKESIDE PEST CONTROL", "Lakeside Pest Control, LLC", "lakeside pest control"])
def test_fuzzy_handles_case_punctuation_suffix(fresh_seed, name):
    r = resolve_vendor(fresh_seed["session"], name, embedder=no_embedder)
    assert r.method == "fuzzy" and r.vendor_name == "Lakeside Pest Control"


def test_embedding_used_when_fuzzy_inconclusive(fresh_seed):
    r = resolve_vendor(fresh_seed["session"], "PolarAir HVAC", embedder=stub_embedder)  # fuzzy ~61
    assert r.method == "embedding" and r.vendor_name == "Polar Air HVAC Services"


def test_embedding_needs_fuzzy_floor(fresh_seed):
    # stub embedder scores this 1.0 against Polar Air, but the names share too little surface form
    r = resolve_vendor(fresh_seed["session"], "Polar Kombucha Brewing Company Texas", embedder=stub_embedder)
    assert r.vendor_id is None


def test_unknown_vendor(fresh_seed):
    r = resolve_vendor(fresh_seed["session"], "Zeta Widgets Corp", embedder=stub_embedder)
    assert r.method == "none" and r.vendor_id is None


def test_embedding_outage_degrades_to_unresolved(fresh_seed):
    def down(texts):
        raise httpx.ConnectError("refused")

    r = resolve_vendor(fresh_seed["session"], "PolarAir HVAC", embedder=down)
    assert r.vendor_id is None and "embedding unavailable" in r.note


# --- live embeddings -----------------------------------------------------------------

from tests.test_llm import _ollama_ready  # noqa: E402


@pytest.mark.ollama
@pytest.mark.skipif(not _ollama_ready(), reason="Ollama not running")
@pytest.mark.parametrize("name,expected", [
    ("Metro Waste", "Metro Waste Haulers"),
    ("Northgate Mechanical Contractors", "Northgate Mechanical"),
    ("Zeta Widgets Corp", None),
])
def test_real_embeddings_resolve_variants(fresh_seed, name, expected):
    r = resolve_vendor(fresh_seed["session"], name)
    assert r.vendor_name == expected, r.candidates[:2]
