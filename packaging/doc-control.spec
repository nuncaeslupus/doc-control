# PyInstaller build file for the one-folder Windows build (PACKAGE T2, freezer D-P6).
#
#   uv sync --locked --extra live --group package
#   .venv/Scripts/pyinstaller --noconfirm --distpath build-out --workpath build-tmp \
#       packaging/doc-control.spec
#
# Output: build-out/doc-control/doc-control.exe plus its runtime files, uncompressed (no
# UPX). .github/workflows/package.yml builds it and fails when the folder is over 40 MiB.
# PyInstaller runs this file as Python, with SPECPATH (this folder) and the build classes
# (Analysis, PYZ, EXE, COLLECT) predefined.

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - predefined by PyInstaller
SRC = ROOT / "src"
PACKAGE = SRC / "doc_control"

# Imports a freezer cannot see. word_com loads pywin32 with importlib on the first live
# call and reads the registry through winreg; the registry imports every
# doc_control.ops.* module by name, and live.dispatch its adapters. Every doc_control
# module is named, so a new ops module is picked up without editing this file.
PYWIN32 = ["pythoncom", "pywintypes", "win32com.client", "win32gui", "winreg"]
OWN = sorted(
    ".".join(path.relative_to(SRC).with_suffix("").parts)
    for path in PACKAGE.rglob("*.py")
    if path.name != "__init__.py"
)
OPS = [name for name in OWN if name.startswith("doc_control.ops.")]
assert len(OPS) > 10, f"doc_control.ops.* modules not found under {PACKAGE}"

# Data files the package reads next to its modules.
DATAS = [
    (str(PACKAGE / "recipes" / "bundled" / "*.json"), "doc_control/recipes/bundled"),
    (str(PACKAGE / "app" / "places.html"), "doc_control/app"),
]

# The exe's icon: docs/logo.png at the sizes Explorer and the taskbar ask for.
ICON = ROOT / "packaging" / "doc-control.ico"

# Never shipped: the GUI toolkit and the test suite (with what only tests import).
EXCLUDES = ["tkinter", "_tkinter", "tests", "pytest", "_pytest"]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(SRC)],
    datas=DATAS,
    hiddenimports=PYWIN32 + OWN,
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="doc-control",
    console=True,
    icon=str(ICON),
    upx=False,
    debug=False,
    strip=False,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    name="doc-control",
    upx=False,
    strip=False,
)
