import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKER_DIR = ROOT / "benchmarks" / "docker"
DOCKERFILE = DOCKER_DIR / "Dockerfile.benchmark-v1"
REQUIREMENTS = DOCKER_DIR / "requirements.benchmark-v1.txt"

EXPECTED_REQUIREMENTS = [
    "fastapi==0.140.0",
    "greenlet==3.5.4",
    "httpx==0.28.1",
    "pytest==9.1.1",
    "ruff==0.16.0",
    "sqlalchemy==2.0.51",
]


def test_benchmark_requirements_are_frozen() -> None:
    assert REQUIREMENTS.exists()
    assert REQUIREMENTS.read_bytes() == ("\n".join(EXPECTED_REQUIREMENTS) + "\n").encode()
    assert EXPECTED_REQUIREMENTS == sorted(EXPECTED_REQUIREMENTS)
    assert all(re.fullmatch(r"[a-z0-9-]+==[0-9]+(?:\.[0-9]+)+", line) for line in EXPECTED_REQUIREMENTS)
    assert "sqlalchemy" not in (DOCKER_DIR / "requirements.ff001.txt").read_text(encoding="utf-8").lower()


def test_benchmark_dockerfile_is_minimal_and_locked_down() -> None:
    assert DOCKERFILE.exists()
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert dockerfile.splitlines()[0] == "FROM python:3.12-slim"
    assert [line for line in dockerfile.splitlines() if line.startswith("COPY ")] == [
        "COPY requirements.benchmark-v1.txt /opt/fastfix/requirements.txt",
        "COPY validation_runner.py /opt/fastfix/validation_runner.py",
    ]
    assert "RUN python -m pip install --no-cache-dir -r /opt/fastfix/requirements.txt" in dockerfile
    assert "ENV PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert 'ENTRYPOINT ["python", "/opt/fastfix/validation_runner.py"]' in dockerfile
    for forbidden in ("apt-get", "curl", "wget", "git"):
        assert forbidden not in dockerfile.lower()
