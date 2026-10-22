import pytest
from sqlmodel import Session

from app.db import init_db, make_engine


@pytest.fixture
def engine():
    eng = make_engine("sqlite://")  # in-memory
    init_db(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def fresh_seed(tmp_path):
    """A freshly seeded DB + data dir that the test may mutate."""
    import json
    from datetime import date

    from scripts.seed_data import seed

    eng = make_engine(f"sqlite:///{tmp_path / 'seed.db'}")
    seed(eng, tmp_path, date(2026, 10, 19))
    manifest = [json.loads(line) for line in (tmp_path / "demo_manifest.jsonl").read_text().splitlines()]
    with Session(eng) as s:
        yield {"session": s, "data_dir": tmp_path, "manifest": manifest}
