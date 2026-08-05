import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _requirement_lines(filename: str) -> list[str]:
    return [
        line.strip()
        for line in (PROJECT_ROOT / filename).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _project_metadata() -> dict:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as file:
        return tomllib.load(file)["project"]


def test_runtime_requirements_match_pyproject() -> None:
    project = _project_metadata()

    assert _requirement_lines("requirements.txt") == project["dependencies"]


def test_optional_requirements_match_pyproject() -> None:
    optional_dependencies = _project_metadata()["optional-dependencies"]

    assert _requirement_lines("requirements-dev.txt") == [
        "-r requirements.txt",
        *optional_dependencies["dev"],
    ]
    assert _requirement_lines("requirements-flower.txt") == [
        "-r requirements.txt",
        *optional_dependencies["monitor"],
    ]
