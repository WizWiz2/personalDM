from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one match, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new), encoding="utf-8")


replace_once(
    "src/backend/tests/run_realistic_simulation_v2.py",
    "    alembic_revision = upgrade_simulation_database(database_path)\n",
    "    alembic_revision = await upgrade_simulation_database(database_path)\n",
)

replace_once(
    "src/backend/tests/test_generative_simulation_overhaul.py",
    '''def test_simulation_database_runs_real_alembic_chain(tmp_path):
    path = tmp_path / "simulation.db"
    revision = upgrade_simulation_database(path)
''',
    '''@pytest.mark.asyncio
async def test_simulation_database_runs_real_alembic_chain(tmp_path):
    path = tmp_path / "simulation.db"
    revision = await upgrade_simulation_database(path)
''',
)
