# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller: API-сервис (FastAPI) как отдельный консольный EXE.

Собирается отдельно от GUI (build.spec) — у сервиса свой набор зависимостей
(fastapi, парсеры документов, SDK провайдеров LLM), и не нужно тащить
PySide6/edifice в EXE, который работает без окна.

Секреты (ключи провайдеров) в бандл не зашиваются: .env читается рядом
с EXE в рантайме (см. build-all.bat — копирует .env в dist/), как и
предполагает pydantic-settings (core/config.py: env_file=".env").
"""

from PyInstaller.utils.hooks import collect_submodules

console = True  # логи выполнения этапов видны только в консоли

# tender_assistant — локальный пакет, не хук pip-пакета: явно просим забрать
# все подмодули, чтобы лениво импортируемые внутри провайдеры LLM
# (ai/model.py: import anthropic / from google import genai — только внутри
# __init__ конкретной модели) точно попали в сборку, а не только то, что
# видно статически из графа импортов main.py.
hidden_imports = collect_submodules('tender_assistant') + [
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'google.genai',
    'anthropic',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Настольный интерфейс в API-сервисе не нужен — свой EXE (build.spec)
        'PySide6', 'edifice',
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
    name='Tender Assistant API',
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
)
