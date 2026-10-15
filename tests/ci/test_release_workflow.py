"""Workflow wiring for release assets (PACKAGE T8), by text scan (no YAML dependency)."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _on_block(text: str) -> str:
    m = re.search(r"^on:\n((?:[ \t]+.*\n|\n)+)", text, re.M)
    assert m, "no top-level on: block"
    return m.group(1)


def _job_block(text: str, job: str) -> str:
    m = re.search(rf"^  {job}:\n((?:    .*\n|\n)+)", text, re.M)
    assert m, f"no job {job}"
    return m.group(1)


def test_only_release_yml_triggers_on_tags() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        on = _on_block(path.read_text(encoding="utf-8"))
        if path.name == "release.yml":
            assert re.search(r"^\s+tags:", on, re.M)
        else:
            assert not re.search(r"^\s+tags:", on, re.M), path.name
    package_on = _on_block((WORKFLOWS / "package.yml").read_text(encoding="utf-8"))
    assert "workflow_call:" in package_on
    assert "push" not in package_on
    assert re.search(r"^\s+attach:", package_on, re.M)


def test_release_calls_package_and_attaches() -> None:
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    package = _job_block(text, "package")
    assert "uses: ./.github/workflows/package.yml" in package
    assert "attach: true" in package
    assert "needs: build" in package
    assert "needs.build.outputs.release == 'true'" in package
    attach = _job_block(text, "attach")
    assert re.search(r"needs: \[[^\]]*package[^\]]*tag[^\]]*\]", attach)
    assert "skipped" in attach and "success" in attach
    assert "--clobber" in attach
    assert "SHA256SUMS.txt" in attach
    assert "## Checksums" in attach
    assert "contents: write" in attach


def test_pypi_publish_untouched() -> None:
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    publish = _job_block(text, "publish")
    assert "needs: build" in publish
    assert "environment:" in publish and "name: pypi" in publish
    assert "pypa/gh-action-pypi-publish" in publish
