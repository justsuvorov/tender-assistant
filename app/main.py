"""Настольный интерфейс конкурсного ассистента.

Собирает пути к исходным материалам, отправляет их в POST /api/update
и показывает сводку по трём этапам: перечень документов, подготовка файлов,
заполнение заявки.
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import edifice
import requests
from edifice import (
    Button,
    HBoxView,
    Label,
    ProgressBar,
    TextInput,
    VBoxView,
    VScrollView,
    Window,
    use_async_call,
    use_state,
)
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QFileDialog

# ── Пути ──────────────────────────────────────────────────────────────────────

# Два разных "рядом": APP_DIR — где лежат РЕДАКТИРУЕМЫЕ файлы (config.json,
# app.log), RESOURCE_DIR — где лежат статичные ресурсы, зашитые в сборку
# PyInstaller (assets). В dev-режиме это одно и то же — папка app/. В собранном
# EXE (frozen) — РАЗНЫЕ места: sys.executable указывает на сам .exe (то, что
# видит и может редактировать пользователь), а sys._MEIPASS — на временную
# распаковку бандла (создаётся заново при каждом запуске и удаляется при
# выходе). Если писать app.log или читать config.json через Path(__file__)
# во frozen-режиме, оба резолвятся ВНУТРЬ _MEIPASS: log потеряется при
# закрытии, а правка config.json рядом с .exe останется незамеченной —
# запущенный процесс её не увидит.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
    BASE_DIR = APP_DIR
else:
    APP_DIR = Path(__file__).parent
    RESOURCE_DIR = APP_DIR
    BASE_DIR = APP_DIR.parent

TENDERS_DIR = BASE_DIR / "tenders"
DOCUMENTS_DIR = BASE_DIR / "documents"
NORMATIVE_DIR = BASE_DIR / "normative_base"
KNOWLEDGE_DIR = BASE_DIR / "knowledge_base"
RESULTS_DIR = BASE_DIR / "results"

_ASSETS = RESOURCE_DIR / "assets"
_LOGO_PATH = _ASSETS / "vsk_logo.png"

# ── Логирование ───────────────────────────────────────────────────────────────

_LOG_PATH = APP_DIR / "app.log"
# Тот же вид строки, что у журнала сервиса — их удобно читать рядом.
_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"

logger = logging.getLogger("tender_assistant.gui")


def _setup_logging() -> None:
    """app.log рядом с программой (5 файлов по 10 МБ) и консоль, если она есть.

    У собранного оконного EXE консоли нет (sys.stderr is None) — тогда только файл.
    """
    from logging.handlers import RotatingFileHandler

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT, "%Y-%m-%d %H:%M:%S")

    handlers = []
    try:
        handlers.append(RotatingFileHandler(
            _LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        ))
    except OSError:
        pass  # нет прав на папку программы — работаем без файла
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))

    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)

# ── Конфигурация ──────────────────────────────────────────────────────────────

_CONFIG_PATH = APP_DIR / "config.json"

_DEFAULT_CONFIG = {
    "api_base_url": "http://localhost:8000",
    "request_timeout": 3600,
}


def _load_config() -> dict:
    config = dict(_DEFAULT_CONFIG)
    if not _CONFIG_PATH.exists():
        return config

    try:
        config.update(json.loads(_CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception as exc:
        logger.warning(f"Не удалось загрузить конфиг: {exc}, используются дефолты")
    return config


_CONFIG = _load_config()
API_URL = _CONFIG["api_base_url"].rstrip("/") + "/api/update"
API_HEALTH_URL = _CONFIG["api_base_url"].rstrip("/") + "/api/health"
REQUEST_TIMEOUT = _CONFIG["request_timeout"]

# ── Стили ─────────────────────────────────────────────────────────────────────

_BG = "#002033"
_CARD = "#0d2e45"
_MUTED = "#8eafc0"
_WHITE = "#ffffff"
_BLUE = "#1a6fa8"
_DIM = "#4a5560"
_GREEN = "#1a7a4a"
_YELLOW = "#c47a1e"
_RED = "#a83a3a"
_ERROR_TEXT = "#ff8f87"  # _RED на тёмном фоне не читается как текст
_INPUT_BG = "#06263a"


def card():
    return {"background-color": _CARD, "border-radius": "8px",
            "padding": "12px", "margin-bottom": "8px"}


def label_s():
    return {"color": _MUTED, "font-size": "13px"}


def value_s(filled: bool = True):
    return {"color": _WHITE if filled else _MUTED, "font-size": "13px"}


def btn(color=_BLUE):
    return {"background-color": color, "color": _WHITE,
            "border-radius": "6px", "padding": "10px 16px", "font-size": "13px"}


# QScrollArea красит вьюпорт палитрой, а не своим стилем: edifice вешает стиль
# селектором QWidget#<id>, который до вьюпорта не достаёт, и тот остаётся
# системно-светлым. Красим вьюпорт и полосы прокрутки на уровне приложения.
_APP_STYLESHEET = f"""
QMainWindow {{ background-color: {_BG}; }}
QScrollArea {{ background-color: {_BG}; border: none; }}
QScrollArea > QWidget > QWidget {{ background-color: {_BG}; }}

QScrollBar:vertical {{
    background: {_BG}; width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {_DIM}; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {_MUTED}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QScrollBar:horizontal {{ background: {_BG}; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{
    background: {_DIM}; border-radius: 5px; min-width: 30px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
"""


# ── Диалоги выбора ────────────────────────────────────────────────────────────

_DOC_FILTER = "Документы (*.docx *.xlsx *.xls *.pdf)"
_FILE_PLACEHOLDER = "Вставьте путь к файлу или нажмите «Выбрать»"
_DIR_PLACEHOLDER = "Вставьте путь к папке или нажмите «Выбрать»"


def _start_dir(current: str, default: Path) -> Path:
    """Диалог открывается там, где уже лежит выбранное, — а не в папке по умолчанию."""
    path = Path(clean_path(current)) if current else None
    if path is not None:
        if path.is_dir():
            return path
        if path.parent.is_dir():
            return path.parent
    return default


def _pick_file(title: str, start_dir: Path, filters: str = _DOC_FILTER) -> str:
    dialog = QFileDialog(None, title, str(start_dir), filters)
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    if dialog.exec():
        files = dialog.selectedFiles()
        return files[0] if files else ""
    return ""


def _pick_directory(title: str, start_dir: Path) -> str:
    dialog = QFileDialog(None, title, str(start_dir))
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    if dialog.exec():
        dirs = dialog.selectedFiles()
        return dirs[0] if dirs else ""
    return ""


def _open_in_explorer(path: str) -> None:
    if not path:
        return
    try:
        os.startfile(path)  # noqa: S606 — открываем результат работы пользователю
    except OSError as exc:
        logger.warning(f"Не удалось открыть {path}: {exc}")


# ── Форматирование ────────────────────────────────────────────────────────────

def _fmt(seconds: int) -> str:
    minutes, secs = divmod(abs(int(seconds)), 60)
    return f"{minutes}:{secs:02d}"


def _fmt_long(seconds: int) -> str:
    hours, rest = divmod(abs(int(seconds)), 3600)
    minutes = rest // 60
    if hours:
        return f"~{hours} ч {minutes} мин"
    return f"~{minutes} мин"


REQUIRED_INPUTS = (
    ("requirements_file", "файл требований конкурса"),
    ("archive_folder", "папку с документами"),
    ("template_file", "шаблон заявки"),
    ("results_folder", "папку для результатов"),
)


def missing_inputs(**inputs) -> list:
    """Незаполненные обязательные поля — человекочитаемыми названиями."""
    return [name for key, name in REQUIRED_INPUTS if not clean_path(inputs.get(key))]


# ── Проверка путей ────────────────────────────────────────────────────────────

class InputPathError(ValueError):
    """Путь в форме не существует или не того типа — запуск не начинается.

    Проверяем до отправки запроса: иначе сервис потратит минуты и токены на
    этапы 1–2 и только потом упадёт на неверной папке, а в пустой папке
    архива «молча» не найдёт ни одного документа.
    """


# (ключ, название поля, что ожидается)
PATH_INPUTS = (
    ("requirements_file", "Требования конкурса", "file"),
    ("archive_folder", "Папка с документами организации", "dir"),
    ("template_file", "Шаблон заявки", "file"),
    ("normative_folder", "Нормативная база", "dir"),
    ("knowledge_folder", "База знаний и эталонные заявки", "dir"),
    ("results_folder", "Папка для результатов", "results"),
)

_SUPPORTED_DOCS = {".docx", ".xlsx", ".xls", ".pdf"}


def clean_path(text) -> str:
    """Путь из поля ввода: без пробелов и кавычек по краям.

    «Копировать как путь» в Проводнике даёт путь в двойных кавычках —
    пользователь вставляет его как есть.
    """
    return str(text or "").strip().strip('"').strip("'").strip()


def path_error(value: str, kind: str) -> str:
    """Что не так с путём; пустая строка — путь в порядке или не указан."""
    value = clean_path(value)
    if not value:
        return ""

    path = Path(value)
    if not path.is_absolute():
        return "Укажите полный путь, например C:\\Конкурсы\\44-2026"

    if kind == "file":
        if path.is_dir():
            return "Указана папка, а нужен файл"
        if not path.is_file():
            return "Файл не найден"
        suffix = path.suffix.lower()
        if suffix == ".doc":
            return "Формат .doc не поддерживается — сохраните файл как .docx"
        if suffix not in _SUPPORTED_DOCS:
            return f"Формат {suffix or 'без расширения'} не поддерживается: нужен .docx, .xlsx, .xls или .pdf"
        return ""

    if path.is_file():
        return "Указан файл, а нужна папка"
    if kind == "results":
        # Папку результатов создадим сами, но только внутри существующей:
        # опечатка в начале пути не должна плодить папки на диске.
        if not path.exists() and not path.parent.is_dir():
            return f"Папка не найдена, и создать её нельзя: нет папки {path.parent}"
        return ""
    if not path.is_dir():
        return "Папка не найдена"
    return ""


def invalid_inputs(**inputs) -> dict:
    """{ключ поля: что не так} по всем заполненным полям."""
    errors = {}
    for key, _, kind in PATH_INPUTS:
        error = path_error(inputs.get(key), kind)
        if error:
            errors[key] = error
    return errors


def validate_inputs(**inputs) -> None:
    """Поднимает InputPathError, если запускать с такими путями нельзя."""
    problems = []
    missing = missing_inputs(**inputs)
    if missing:
        problems.append("укажите " + ", ".join(missing))

    titles = {key: title for key, title, _ in PATH_INPUTS}
    for key, error in invalid_inputs(**inputs).items():
        problems.append(f"{titles[key]}: {error.lower()}")

    if problems:
        raise InputPathError("Проверьте пути — " + "; ".join(problems))


def _result_summary(data: dict) -> str:
    """Итоги трёх этапов одной строкой — для журнала."""
    documents = (data.get("document_list") or {}).get("documents") or []
    rows = (data.get("prepared_documents") or {}).get("rows") or []
    fields = (data.get("application") or {}).get("fields") or []

    def count(items, status):
        return sum(1 for item in items if item.get("status") == status)

    return (
        f"Перечень: {len(documents)} док.; "
        f"комплект: есть {count(rows, 'Есть')}, проверить {count(rows, 'Проверить')}, "
        f"нет {count(rows, 'Нет')}; "
        f"заявка: найдено {count(fields, 'found')}, проверить {count(fields, 'check')}, "
        f"нет {count(fields, 'missing')}"
    )


def build_payload(
    requirements_file: str,
    archive_folder: str,
    template_file: str,
    results_folder: str,
    normative_folder: str = "",
    knowledge_folder: str = "",
    message_id: int = None,
) -> dict:
    """Тело запроса POST /api/update.

    Ключи обязаны совпадать с ``tender_assistant.models.request.APIRequest`` —
    это проверяется тестом, иначе рассинхрон вылезет только в рантайме.
    """
    return {
        "message_id": int(time.time()) if message_id is None else message_id,
        "priority": 1,
        "file_path": requirements_file,
        "documents_folder_path": archive_folder,
        "application_template_path": template_file,
        "results_path": results_folder,
        "normative_base_folder": normative_folder or None,
        "knowledge_base_folder": knowledge_folder or None,
        "result_folder_name": Path(requirements_file).stem or "komplekt",
    }


def _error_detail(response) -> str:
    """Разворачивает {"detail": ...} из FastAPI в читаемое сообщение."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]

    detail = payload.get("detail", payload)
    if isinstance(detail, list) and detail:
        first = detail[0]
        location = " → ".join(str(p) for p in first.get("loc", [])[1:])
        return f"{location}: {first.get('msg', '')}"
    return str(detail)[:300]


# ── Строка выбора пути ────────────────────────────────────────────────────────

def _input_s(error: bool) -> dict:
    return {"background-color": _INPUT_BG, "color": _WHITE, "font-size": "13px",
            "padding": "6px 8px", "border-radius": "6px", "margin-right": "8px",
            "border": f"1px solid {_RED if error else _DIM}"}


def _picker_row(title: str, value: str, placeholder: str, on_pick, on_change,
                error: str = "", hint: str = ""):
    """Поле с путём: можно вставить путь вручную или выбрать в диалоге."""
    with VBoxView(style=card()):
        Label(text=title, style={**label_s(), "margin-bottom": "6px"})
        with HBoxView():
            TextInput(text=value, placeholder_text=placeholder,
                      on_change=on_change, style=_input_s(bool(error)))
            Button(title="Выбрать", on_click=on_pick, style=btn())
        if error:
            Label(text=error, word_wrap=True,
                  style={"color": _ERROR_TEXT, "font-size": "12px", "margin-top": "6px"})
        elif hint:
            Label(text=hint, style={**label_s(), "font-size": "11px",
                                    "margin-top": "6px"})


def _stat_row(caption: str, value: str, colour: str = _WHITE):
    with HBoxView(style={"margin-bottom": "4px"}):
        Label(text=caption, style={**label_s(), "margin-right": "12px"})
        Label(text=value, style={"color": colour, "font-size": "13px"})


# ── Компонент ─────────────────────────────────────────────────────────────────

@edifice.component
def TenderAssistantApp(self):
    requirements_file, set_requirements_file = use_state("")
    archive_folder,    set_archive_folder    = use_state("")
    template_file,     set_template_file     = use_state("")
    normative_folder,  set_normative_folder  = use_state("")
    knowledge_folder,  set_knowledge_folder  = use_state("")
    results_folder,    set_results_folder    = use_state(str(RESULTS_DIR))

    status,     set_status     = use_state("Готов к работе")
    processing, set_processing = use_state(False)
    elapsed,    set_elapsed    = use_state(0)
    result,     set_result     = use_state(None)

    # ── Выбор путей ───────────────────────────────────────────────────────

    def _reset_result():
        set_result(None)
        set_status("Готов к работе")

    def pick_requirements(_=None):
        path = _pick_file("Требования конкурса", _start_dir(requirements_file, TENDERS_DIR))
        if path:
            set_requirements_file(path)
            logger.info(f"Выбрано — Требования конкурса: {path}")
            _reset_result()

    def pick_archive(_=None):
        path = _pick_directory("Папка с документами организации", _start_dir(archive_folder, DOCUMENTS_DIR))
        if path:
            set_archive_folder(path)
            logger.info(f"Выбрано — Папка с документами организации: {path}")
            _reset_result()

    def pick_template(_=None):
        path = _pick_file("Шаблон заявки", _start_dir(template_file, TENDERS_DIR))
        if path:
            set_template_file(path)
            logger.info(f"Выбрано — Шаблон заявки: {path}")
            _reset_result()

    def pick_normative(_=None):
        path = _pick_directory("Нормативная база", _start_dir(normative_folder, NORMATIVE_DIR))
        if path:
            set_normative_folder(path)
            logger.info(f"Выбрано — Нормативная база: {path}")
            _reset_result()

    def pick_knowledge(_=None):
        path = _pick_directory("База знаний и эталонные заявки", _start_dir(knowledge_folder, KNOWLEDGE_DIR))
        if path:
            set_knowledge_folder(path)
            logger.info(f"Выбрано — База знаний и эталонные заявки: {path}")
            _reset_result()

    def pick_results(_=None):
        path = _pick_directory("Папка для результатов", _start_dir(results_folder, RESULTS_DIR))
        if path:
            set_results_folder(path)
            logger.info(f"Выбрано — Папка для результатов: {path}")
            _reset_result()

    def typed(setter):
        """Путь, набранный или вставленный вручную в поле ввода."""
        def on_change(text: str):
            setter(text)
            _reset_result()
        return on_change

    # ── Запуск обработки ──────────────────────────────────────────────────

    def _inputs() -> dict:
        return {key: clean_path(value) for key, value in {
            "requirements_file": requirements_file,
            "archive_folder": archive_folder,
            "template_file": template_file,
            "results_folder": results_folder,
            "normative_folder": normative_folder,
            "knowledge_folder": knowledge_folder,
        }.items()}

    async def _process_async():
        try:
            validate_inputs(**_inputs())
        except InputPathError as exc:
            set_status(str(exc))
            logger.warning(f"Запуск отклонён: {exc}")
            return

        set_processing(True)
        set_result(None)
        set_elapsed(0)
        set_status("Обработка: анализ требований...")

        started = time.time()

        async def tick():
            while True:
                await asyncio.sleep(1)
                set_elapsed(int(time.time() - started))

        tick_task = asyncio.create_task(tick())

        try:
            Path(results_folder).mkdir(parents=True, exist_ok=True)
            payload = build_payload(**_inputs())
            logger.info(
                f"Запрос #{payload['message_id']} → {API_URL}:\n"
                + json.dumps(payload, ensure_ascii=False, indent=2)
            )

            response = await asyncio.to_thread(
                requests.post, API_URL, json=payload, timeout=REQUEST_TIMEOUT
            )

            elapsed_text = _fmt_long(time.time() - started)
            if response.ok:
                data = response.json()
                set_result(data)
                logger.info(f"Ответ 200 за {elapsed_text}. {_result_summary(data)}")
                errors = data.get("errors") or {}
                if errors:
                    set_status(f"Выполнено частично за {elapsed_text} — "
                               f"см. ошибки этапов ниже")
                    for stage, error in errors.items():
                        logger.warning(f"Этап {stage} не выполнен: {error}")
                else:
                    set_status(f"Готово за {elapsed_text}")
            else:
                message = _error_detail(response)
                set_status(f"Ошибка {response.status_code}: {message}")
                logger.error(
                    f"Ответ {response.status_code} за {elapsed_text}: {message}"
                )

        except requests.ConnectionError as exc:
            set_status(
                f"Сервис недоступен: {API_URL}. Запустите "
                f"«Tender Assistant API.exe» и повторите"
            )
            logger.error(f"Не удалось подключиться к сервису {API_URL}: {exc}")
        except requests.Timeout:
            set_status("Превышено время ожидания ответа сервиса")
            logger.error(
                f"Сервис не ответил за {REQUEST_TIMEOUT} с (request_timeout в config.json)"
            )
        except Exception as exc:
            set_status(f"Ошибка: {str(exc)[:200]}")
            logger.exception("Непредвиденная ошибка")
        finally:
            tick_task.cancel()
            set_processing(False)

    process, _ = use_async_call(_process_async)

    def on_process(_=None):
        if not processing:
            process()

    # ── Отрисовка ─────────────────────────────────────────────────────────

    errors = invalid_inputs(**_inputs())
    ready = not missing_inputs(**_inputs()) and not errors

    with Window(title="Конкурсный ассистент",
                style={"background-color": _BG,
                       "min-width": "760px", "min-height": "640px"}):
        with VScrollView(style={"background-color": _BG, "padding": "20px"}):

            with HBoxView(style={"margin-bottom": "16px", "align": "left"}):
                if _LOGO_PATH.exists():
                    edifice.Image(src=str(_LOGO_PATH),
                                  style={"width": "44px", "height": "44px",
                                         "margin-right": "12px"})
                Label(text="Конкурсный ассистент",
                      style={"color": _WHITE, "font-size": "20px",
                             "font-weight": "bold"})

            # ── Исходные материалы ────────────────────────────────────────

            Label(text="ИСХОДНЫЕ МАТЕРИАЛЫ",
                  style={**label_s(), "font-size": "11px",
                         "font-weight": "bold", "margin-bottom": "8px"})

            _picker_row("Требования конкурса", requirements_file, _FILE_PLACEHOLDER,
                        pick_requirements, typed(set_requirements_file),
                        errors.get("requirements_file", ""),
                        "Документ, из которого берётся перечень документов")
            _picker_row("Папка с документами организации", archive_folder,
                        _DIR_PLACEHOLDER, pick_archive, typed(set_archive_folder),
                        errors.get("archive_folder", ""),
                        "Архив, где ищутся файлы для комплекта заявки")
            _picker_row("Шаблон заявки", template_file, _FILE_PLACEHOLDER,
                        pick_template, typed(set_template_file),
                        errors.get("template_file", ""),
                        "Форма, которую нужно заполнить")

            # ── Базы знаний ───────────────────────────────────────────────

            Label(text="БАЗЫ ЗНАНИЙ (НЕОБЯЗАТЕЛЬНО)",
                  style={**label_s(), "font-size": "11px", "font-weight": "bold",
                         "margin-top": "8px", "margin-bottom": "8px"})

            _picker_row("Нормативная база", normative_folder, _DIR_PLACEHOLDER,
                        pick_normative, typed(set_normative_folder),
                        errors.get("normative_folder", ""),
                        "По ней проверяется перечень документов")
            _picker_row("База знаний и эталонные заявки", knowledge_folder,
                        _DIR_PLACEHOLDER, pick_knowledge, typed(set_knowledge_folder),
                        errors.get("knowledge_folder", ""),
                        "Из неё берутся значения для заполнения заявки")

            # ── Результаты ────────────────────────────────────────────────

            Label(text="РЕЗУЛЬТАТ",
                  style={**label_s(), "font-size": "11px", "font-weight": "bold",
                         "margin-top": "8px", "margin-bottom": "8px"})

            _picker_row("Папка для результатов", results_folder, _DIR_PLACEHOLDER,
                        pick_results, typed(set_results_folder),
                        errors.get("results_folder", ""),
                        "Сюда попадут комплект документов и отчёты")

            Button(
                title="Обработка..." if processing else "Подготовить заявку",
                on_click=on_process,
                style={**btn(_DIM if processing or not ready else _BLUE),
                       "margin-top": "8px", "margin-bottom": "10px",
                       "font-size": "14px"},
            )

            # ── Статус ────────────────────────────────────────────────────

            with VBoxView(style=card()):
                Label(text="Статус", style={**label_s(), "margin-bottom": "6px"})
                Label(text=status, style={**value_s(), "margin-bottom": "6px"})

                if processing:
                    # Длительность заранее неизвестна — показываем «занят».
                    ProgressBar(value=0, min_value=0, max_value=0,
                                style={"height": "8px", "margin-bottom": "6px"})
                    Label(text=f"Прошло: {_fmt(elapsed)}", style=label_s())

            if result:
                _results_view(result)


def _stage_error(message):
    """Сбой этапа: остальные этапы выполнены, этот — нет."""
    if message:
        Label(text=f"Этап не выполнен: {message}", word_wrap=True,
              style={**label_s(), "color": _RED, "margin-bottom": "8px"})


def _results_view(result: dict):
    """Сводка по трём этапам плюс кнопки открытия файлов."""
    document_list = result.get("document_list") or {}
    prepared = result.get("prepared_documents") or {}
    application = result.get("application") or {}
    errors = result.get("errors") or {}

    documents = document_list.get("documents") or []
    excluded = document_list.get("excluded") or []
    rows = prepared.get("rows") or []
    fields = application.get("fields") or []

    def count_rows(status: str) -> int:
        return sum(1 for row in rows if row.get("status") == status)

    def count_fields(status: str) -> int:
        return sum(1 for field in fields if field.get("status") == status)

    # Этап 1
    with VBoxView(style=card()):
        Label(text="1. Перечень документов",
              style={**value_s(), "font-weight": "bold", "margin-bottom": "8px"})
        _stage_error(errors.get("document_list"))
        _stat_row("Требуется документов:", str(len(documents)))
        if excluded:
            _stat_row("Исключено по нормативной базе:", str(len(excluded)), _MUTED)

        with HBoxView(style={"margin-top": "8px"}):
            if document_list.get("formatted_path"):
                Button(title="Открыть перечень",
                       on_click=lambda _=None: _open_in_explorer(
                           document_list["formatted_path"]),
                       style=btn(_GREEN))
            if document_list.get("report_path"):
                Button(title="Открыть отчёт",
                       on_click=lambda _=None: _open_in_explorer(
                           document_list["report_path"]),
                       style=btn())

    # Этап 2
    with VBoxView(style=card()):
        Label(text="2. Подготовка документов",
              style={**value_s(), "font-weight": "bold", "margin-bottom": "8px"})
        _stage_error(errors.get("prepared_documents"))
        _stat_row("Есть:", str(count_rows("Есть")), _GREEN)
        _stat_row("Проверить:", str(count_rows("Проверить")), _YELLOW)
        _stat_row("Нет:", str(count_rows("Нет")), _RED)

        with HBoxView(style={"margin-top": "8px"}):
            if prepared.get("result_folder"):
                Button(title="Открыть комплект",
                       on_click=lambda _=None: _open_in_explorer(
                           prepared["result_folder"]),
                       style=btn(_GREEN))
            if prepared.get("report_path"):
                Button(title="Открыть отчёт",
                       on_click=lambda _=None: _open_in_explorer(
                           prepared["report_path"]),
                       style=btn())

    # Этап 3
    with VBoxView(style=card()):
        Label(text="3. Заполнение заявки",
              style={**value_s(), "font-weight": "bold", "margin-bottom": "8px"})
        _stage_error(errors.get("application"))
        _stat_row("Заполнено:", str(count_fields("found")), _GREEN)
        _stat_row("Требует проверки:", str(count_fields("check")), _YELLOW)
        _stat_row("Не найдено:", str(count_fields("missing")), _RED)
        Label(text="В документе значения выделены зелёным, жёлтым и красным",
              style={**label_s(), "font-size": "11px", "margin-top": "6px"})

        with HBoxView(style={"margin-top": "8px"}):
            if application.get("filled_path"):
                Button(title="Открыть заявку",
                       on_click=lambda _=None: _open_in_explorer(
                           application["filled_path"]),
                       style=btn(_GREEN))
            if application.get("report_path"):
                Button(title="Открыть отчёт",
                       on_click=lambda _=None: _open_in_explorer(
                           application["report_path"]),
                       style=btn())


# ── Точка входа ───────────────────────────────────────────────────────────────

def _apply_theme(qapp) -> None:
    """Тёмный фон вьюпорта скролла и полос прокрутки, затем шрифт поверх."""
    if not qapp:
        return

    stylesheet = _APP_STYLESHEET
    font_path = _ASSETS / "GPN_DIN_Condensed-Regular.ttf"
    if font_path.exists():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id != -1:
            family = QFontDatabase.applicationFontFamilies(font_id)[0]
            qapp.setFont(QFont(family, 11))
            stylesheet += f"\n* {{ font-family: '{family}'; }}"

    qapp.setStyleSheet(stylesheet)


if __name__ == "__main__":
    _setup_logging()
    logger.info(f"Запуск интерфейса, API: {API_URL}, журнал: {_LOG_PATH}")

    app = edifice.App(TenderAssistantApp())
    _apply_theme(QApplication.instance())
    app.start()
