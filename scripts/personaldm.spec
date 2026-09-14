# -*- mode: python ; coding: utf-8 -*-
# Opaque PersonalDM Windows build (onedir). Sources stay in the repo; the zip ships only the exe bundle.

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

REPO = Path(SPECPATH).resolve().parent
BACKEND = REPO / "src" / "backend"
FRONTEND_DIST = REPO / "src" / "frontend" / "dist"

if not (FRONTEND_DIST / "index.html").is_file():
    raise SystemExit(f"Frontend dist missing: {FRONTEND_DIST}")

datas = [
    (str(BACKEND / "alembic"), "alembic"),
    (str(BACKEND / "alembic.ini"), "."),
    (str(FRONTEND_DIST), str(Path("frontend") / "dist")),
]

hiddenimports = []
binaries = []

for pkg in ("uvicorn", "fastapi", "starlette", "anyio", "httpx", "pydantic", "sqlalchemy", "alembic", "aiosqlite", "cryptography", "questionary", "multipart"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hidden
    except Exception:
        hiddenimports += collect_submodules(pkg)

hiddenimports += collect_submodules("app")
hiddenimports += [
    "app.db.migration_compat",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "cli",
    "cli_tui",
    "launcher",
    "packaged_entry",
]

a = Analysis(
    [str(BACKEND / "packaged_entry.py")],
    pathex=[str(BACKEND)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "ruff", "mypy"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PersonalDM",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PersonalDM",
)
