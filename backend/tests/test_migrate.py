import psycopg
import pytest

from app.common.migrate import MIGRATIONS_DIR, apply_migrations, discover


def test_migration_files_are_numbered_and_contiguous():
    names = [f.name for f in discover()]
    assert names
    assert [int(n[:4]) for n in names] == list(range(1, len(names) + 1))


def test_bad_file_name_is_rejected(tmp_path):
    (tmp_path / "init.sql").write_text("select 1")
    with pytest.raises(ValueError, match="0001_name.sql"):
        discover(tmp_path)


def test_applies_once_and_rerun_is_a_no_op(database_url):
    first = apply_migrations(database_url)
    assert first == [f.name for f in discover(MIGRATIONS_DIR)]
    assert apply_migrations(database_url) == []
    with psycopg.connect(database_url) as conn:
        tables = {
            r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        }
        hypertables = {
            r[0]
            for r in conn.execute("SELECT hypertable_name FROM timescaledb_information.hypertables")
        }
    assert {"markets", "candles", "quotes", "schema_migrations"} <= tables
    assert hypertables == {"candles", "quotes"}


def test_a_failing_migration_rolls_back_and_is_not_recorded(database_url, tmp_path):
    (tmp_path / "0001_ok.sql").write_text("CREATE TABLE a (x int);")
    (tmp_path / "0002_bad.sql").write_text("CREATE TABLE b (x int); SELECT nope();")
    with pytest.raises(psycopg.Error):
        apply_migrations(database_url, tmp_path)
    with psycopg.connect(database_url) as conn:
        assert [r[0] for r in conn.execute("SELECT name FROM schema_migrations")] == ["0001_ok.sql"]
        assert conn.execute("SELECT to_regclass('b')").fetchone()[0] is None
