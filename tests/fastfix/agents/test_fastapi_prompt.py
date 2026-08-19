from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROMPTS = [
    ROOT / "src" / "fastfix" / "config" / "diagnosis.yaml",
    ROOT / "src" / "fastfix" / "config" / "repair.yaml",
]


def test_prompts_add_only_generic_route_analysis_guidance() -> None:
    for path in PROMPTS:
        prompt = path.read_text(encoding="utf-8")
        assert "you may use inspect_fastapi_routes" in prompt
        assert "awaited_calls and unawaited_calls as static observations" in prompt
        assert "do not call it mechanically for every task" in prompt
        assert "fetch_user" not in prompt
        assert "app/main.py" not in prompt
        assert "return await" not in prompt
