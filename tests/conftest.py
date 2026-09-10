from pathlib import Path


def pytest_sessionstart(session):
    project_temp = Path(__file__).resolve().parents[1] / "temp"
    project_temp.mkdir(parents=True, exist_ok=True)
