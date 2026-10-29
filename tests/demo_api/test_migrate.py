"""Migration runner rules that need no database: the transaction opt-out header
and the file naming the runner applies."""

from app import migrate


def test_files_run_in_a_transaction_unless_the_first_line_opts_out():
    assert migrate.runs_in_transaction("CREATE TABLE t (id int);")
    assert migrate.runs_in_transaction("-- a comment\n-- migrate: no-transaction\nCREATE INDEX ...")
    assert migrate.runs_in_transaction("")
    assert not migrate.runs_in_transaction("-- migrate: no-transaction\nCREATE INDEX CONCURRENTLY i ON t (c);")
    assert not migrate.runs_in_transaction("\n  -- migrate: no-transaction  \nCREATE INDEX CONCURRENTLY i ON t (c);")


def test_shipped_migrations_are_numbered_and_transactional():
    files = sorted(migrate.MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    assert [f.stem for f in files] == ["0001_initial"]
    assert all(migrate.runs_in_transaction(f.read_text()) for f in files)
