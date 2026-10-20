from __future__ import annotations

from pathlib import Path

from omniimmune.healer.patcher import LLMFallbackPatcher


SOURCE = "def handle(value):\n    return value + 1\n"
DIFF = """```diff
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def handle(value):
-    return value + 1
+    return int(value) + 1
```
"""


def test_synthesize_patch_strips_markdown_validates_and_compiles(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "app.py"
    target.write_text(SOURCE, encoding="utf-8")
    prompts: list[str] = []

    def llm(prompt: str) -> str:
        prompts.append(prompt)
        return DIFF

    monkeypatch.setenv("OMNI_LLM_KEY", "test-key")
    patcher = LLMFallbackPatcher(target, llm_call=llm)
    diff = patcher.synthesize_patch(SOURCE, "app.py", 2, "ValueError: bad", {"value": "x"})

    assert diff is not None
    assert diff.startswith("--- a/app.py\n+++ b/app.py")
    assert "Output ONLY a valid unified diff" in prompts[0]
    assert "return int(value) + 1" in (patcher.last_modified_source or "")
    compile(patcher.last_modified_source or "", str(target), "exec")


def test_synthesize_patch_requires_credentials_and_rejects_invalid_syntax(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "app.py"
    target.write_text(SOURCE, encoding="utf-8")
    called = False

    def llm(_: str) -> str:
        nonlocal called
        called = True
        return "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def handle(value):\n-    return value + 1\n+    return (\n"

    monkeypatch.delenv("OMNI_LLM_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert LLMFallbackPatcher(target, llm_call=llm).synthesize_patch(SOURCE, "app.py", 1, "", {}) is None
    assert called is False

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert LLMFallbackPatcher(target, llm_call=llm).synthesize_patch(SOURCE, "app.py", 1, "", {}) is None
