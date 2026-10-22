from app.config import fraud_config, get_settings, policy_config, rails_config
from app.models import Rail


def test_rails_cover_every_rail_enum():
    assert set(rails_config()["rails"]) == {r.value for r in Rail}


def test_money_values_are_integer_cents():
    for name, rail in rails_config()["rails"].items():
        assert isinstance(rail["fee_cents"], int), name
        assert rail.get("max_cents") is None or isinstance(rail["max_cents"], int), name
    p = policy_config()
    assert isinstance(p["auto_pay"]["max_amount_cents"], int)
    assert isinstance(p["dual_approval"]["min_amount_cents"], int)
    assert isinstance(p["dual_approval"]["instant_min_amount_cents"], int)


def test_policy_signals_exist_in_fraud_config():
    signals = set(fraud_config()["signals"])
    p = policy_config()
    assert set(p["hard_blocks"]) <= signals
    assert set(p["hold_until_callback"]) <= signals
    assert set(p["force_review"]) <= signals


def test_duplicates_reviewed_not_blocked():
    p = policy_config()
    assert "DUPLICATE_INVOICE" in p["force_review"]
    assert "DUPLICATE_INVOICE" not in p["hard_blocks"]


def test_settings_defaults():
    s = get_settings()
    assert s.max_agent_steps == 5
    assert s.db_path.is_absolute()
