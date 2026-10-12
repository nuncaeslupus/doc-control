"""The hygiene gate flags private-corpus identifiers without echoing them."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "hygiene_denylist.py"
TERM = "zzprivatetitle"


def _run(repo: Path, denylist: Path | None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"]}
    if denylist is not None:
        env["DOC_CONTROL_HYGIENE_DENYLIST"] = str(denylist)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(repo)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name, text in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    return repo


def test_hygiene_flags_denylisted_name(tmp_path: Path) -> None:
    deny = tmp_path / "deny.txt"
    deny.write_text(f"# private titles\n{TERM}\n", encoding="utf-8")
    repo = _repo(
        tmp_path,
        {"src/a.py": f"# notes on {TERM.upper()}\n", f"docs/{TERM}.md": "x\n", "ok.txt": "fine\n"},
    )
    result = _run(repo, deny)
    assert result.returncode == 1
    assert "private_corpus_identifiers_in_repo=2" in result.stdout
    assert "src/a.py:1" in result.stdout
    # The term may appear only inside a reported path, never as matched text.
    assert TERM not in result.stdout.lower().replace(f"docs/{TERM}.md", "")


def test_hygiene_flags_home_directory_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"a.py": 'P = "/home/jane/manuscripts/"\n'})
    result = _run(repo, None)
    assert result.returncode == 1
    assert "jane" not in result.stdout


def test_hygiene_passes_clean_repo_and_reports_zero(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"a.py": 'P = "/home/user/x/"\n'})
    result = _run(repo, None)
    assert result.returncode == 0
    assert "private_corpus_identifiers_in_repo=0" in result.stdout


def test_private_corpus_identifiers_in_repo_is_zero() -> None:
    """Gate metric: the real checkout, built-in rules (CI has no denylist)."""
    spec = importlib.util.spec_from_file_location("hygiene_denylist", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    count = len(module.scan(ROOT, []))
    print(f"private_corpus_identifiers_in_repo={count}")
    assert count == 0


def test_hygiene_script_is_wired_into_hygiene_sh() -> None:
    assert "hygiene_denylist.py" in (ROOT / "scripts" / "hygiene.sh").read_text(encoding="utf-8")


@pytest.mark.parametrize("job", ["validate", "libreoffice", "smoke-no-network", "bench"])
def test_ci_workflow_defines_extra_jobs(job: str) -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f"\n  {job}:\n" in text
