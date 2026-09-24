from __future__ import annotations

from pathlib import Path

from sheetsage2_service.model_runtime import stage_local_remote_code


def test_local_snapshot_code_is_staged_with_transitive_relative_imports(tmp_path: Path):
    # transformers 4.45 copies only the model file's direct relative imports of
    # a local snapshot, then resolves them transitively: exports_sheetsage2.py
    # imports chord_spelling_sheetsage2.py, which was never copied (job
    # 6ac584dff206 failed with FileNotFoundError on it).
    snapshot = tmp_path / "models" / "sheetsage2"
    snapshot.mkdir(parents=True)
    (snapshot / "__init__.py").write_text('"""upstream package docstring"""\n', encoding="utf-8")
    (snapshot / "modeling_sheetsage2.py").write_text("from .exports_sheetsage2 import x\n", encoding="utf-8")
    (snapshot / "exports_sheetsage2.py").write_text("from .chord_spelling_sheetsage2 import y\n", encoding="utf-8")
    (snapshot / "chord_spelling_sheetsage2.py").write_text("y = 1\n", encoding="utf-8")
    (snapshot / "model.safetensors").write_bytes(b"weights")

    modules_root = tmp_path / "hf" / "modules"
    staged = stage_local_remote_code(snapshot, modules_root=modules_root)

    assert staged == modules_root / "transformers_modules" / "sheetsage2"
    for name in ("modeling_sheetsage2.py", "exports_sheetsage2.py", "chord_spelling_sheetsage2.py"):
        assert (staged / name).read_bytes() == (snapshot / name).read_bytes()
    assert not (staged / "model.safetensors").exists()
    assert (modules_root / "__init__.py").is_file()
    assert (modules_root / "transformers_modules" / "__init__.py").is_file()
    assert (staged / "__init__.py").read_text(encoding="utf-8") == ""
