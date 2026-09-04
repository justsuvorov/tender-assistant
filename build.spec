# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller: настольный интерфейс (edifice/PySide6) как отдельный EXE.

Собирается отдельно от API (build_api.spec) — GUI ничего не знает о FastAPI
и общается с сервисом только по HTTP (см. app/main.py, API_URL).

app/assets/ (логотип, шрифт) в репозитории может отсутствовать — оба
обращения к нему в app/main.py уже защищены проверкой .exists(), поэтому
здесь тоже включаем datas только если папка реально на диске, а не падаем
на сборке из-за отсутствующего пути. Бандлится в корень распаковки
('assets', не 'app/assets') — так же плоско, как app/main.py ищет её через
RESOURCE_DIR (= sys._MEIPASS в frozen-режиме, без вложенной папки app/).

config.json НЕ бандлится: это внешний, редактируемый без пересборки файл
(app/main.py читает его через APP_DIR = папка рядом с .exe, а не из бандла) —
его в dist/ кладёт build-all.bat, как и .env для API.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

console = False  # окно без консоли — это настольное приложение, не сервис

assets_dir = Path('app/assets')
datas = []
if assets_dir.is_dir():
    datas.append((str(assets_dir), 'assets'))

icon_path = assets_dir / 'vsk_logo.ico'
icon = str(icon_path) if icon_path.is_file() else None

hidden_imports = collect_submodules('PySide6') + [
    'edifice',
    'requests',
    'loguru',
]

a = Analysis(
    ['app/main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Серверная часть в GUI не нужна — свой EXE (build_api.spec)
        'fastapi', 'uvicorn',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Tender Assistant',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=console,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)
