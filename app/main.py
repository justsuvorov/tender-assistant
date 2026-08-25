"""Настольный интерфейс тендер-ассистента.

Собирает пути к исходным материалам, отправляет их в POST /api/update
и показывает сводку по трём этапам: перечень документов, подготовка файлов,
заполнение заявки.
"""

import asyncio
import json
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
    VBoxView,
    VScrollView,
    Window,
    use_async_call,
    use_state,
)
from loguru import logger
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QFileDialog

# ── Пути ──────────────────────────────────────────────────────────────────────

# В собранном EXE рабочей папкой считается текущая, в разработке — корень проекта.
if getattr(sys, "frozen", False):
    BASE_DIR = Path.cwd()
else:
    BASE_DIR = Path(__file__).parent.parent

TENDERS_DIR = BASE_DIR / "tenders"
DOCUMENTS_DIR = BASE_DIR / "documents"
NORMATIVE_DIR = BASE_DIR / "normative_base"
KNOWLEDGE_DIR = BASE_DIR / "knowledge_base"
RESULTS_DIR = BASE_DIR / "results"

_ASSETS = Path(__file__).parent / "assets"
_LOGO_PATH = _ASSETS / "vsk_logo.png"

# ── Логирование ───────────────────────────────────────────────────────────────

_LOG_PATH = Path(__file__).parent / "app.log"
_LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}"

logger.remove()
logger.add(_LOG_PATH, format=_LOG_FORMAT, rotation="10 MB")
logger.add(lambda msg: print(msg, end=""), format=_LOG_FORMAT)

# ── Конфигурация ──────────────────────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).parent / "config.json"

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

_DOC_FILTER = "Документы (*.docx *.doc *.xlsx *.xls *.pdf)"


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


def _short(path: str, placeholder: str) -> str:
    return Path(path).name if path else placeholder


REQUIRED_INPUTS = (
    ("requirements_file", "файл требований тендера"),
    ("archive_folder", "папку с документами"),
    ("template_file", "шаблон заявки"),
    ("results_folder", "папку для результатов"),
)


def missing_inputs(**inputs) -> list:
    """Незаполненные обязательные поля — человекочитаемыми названиями."""
    return [name for key, name in REQUIRED_INPUTS if not inputs.get(key)]


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

def _picker_row(title: str, value: str, placeholder: str, on_pick, hint: str = ""):
    with VBoxView(style=card()):
        Label(text=title, style={**label_s(), "margin-bottom": "6px"})
        with HBoxView():
            Label(text=_short(value, placeholder),
                  style=value_s(filled=bool(value)))
            Button(title="Выбрать", on_click=on_pick, style=btn())
        if hint:
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
        path = _pick_file("Требования тендера", TENDERS_DIR)
        if path:
            set_requirements_file(path)
            _reset_result()

    def pick_archive(_=None):
        path = _pick_directory("Папка с документами организации", DOCUMENTS_DIR)
        if path:
            set_archive_folder(path)
            _reset_result()

    def pick_template(_=None):
        path = _pick_file("Шаблон заявки", TENDERS_DIR)
        if path:
            set_template_file(path)
            _reset_result()

    def pick_normative(_=None):
        path = _pick_directory("Нормативная база", NORMATIVE_DIR)
        if path:
            set_normative_folder(path)
            _reset_result()

    def pick_knowledge(_=None):
        path = _pick_directory("База знаний и эталонные заявки", KNOWLEDGE_DIR)
        if path:
            set_knowledge_folder(path)
            _reset_result()

    def pick_results(_=None):
        path = _pick_directory("Папка для результатов", RESULTS_DIR)
        if path:
            set_results_folder(path)
            _reset_result()

    # ── Запуск обработки ──────────────────────────────────────────────────

    def _inputs() -> dict:
        return {
            "requirements_file": requirements_file,
            "archive_folder": archive_folder,
            "template_file": template_file,
            "results_folder": results_folder,
            "normative_folder": normative_folder,
            "knowledge_folder": knowledge_folder,
        }

    def _missing_inputs() -> list:
        return missing_inputs(**_inputs())

    async def _process_async():
        missing = _missing_inputs()
        if missing:
            set_status("Укажите: " + ", ".join(missing))
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
            logger.info(f"Запрос: {payload}")

            response = await asyncio.to_thread(
                requests.post, API_URL, json=payload, timeout=REQUEST_TIMEOUT
            )

            if response.ok:
                data = response.json()
                set_result(data)
                set_status(f"Готово за {_fmt_long(time.time() - started)}")
                logger.info("Обработка завершена успешно")
            else:
                message = _error_detail(response)
                set_status(f"Ошибка {response.status_code}: {message}")
                logger.error(f"Ошибка API {response.status_code}: {message}")

        except requests.ConnectionError:
            set_status(
                f"Сервис недоступен: {API_URL}. Запустите его командой "
                f"«uvicorn main:app»"
            )
            logger.error("Не удалось подключиться к API")
        except requests.Timeout:
            set_status("Превышено время ожидания ответа сервиса")
            logger.error("Таймаут запроса к API")
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

    ready = not _missing_inputs()

    with Window(title="Тендер-ассистент",
                style={"background-color": _BG,
                       "min-width": "760px", "min-height": "640px"}):
        with VScrollView(style={"background-color": _BG, "padding": "20px"}):

            with HBoxView(style={"margin-bottom": "16px", "align": "left"}):
                if _LOGO_PATH.exists():
                    edifice.Image(src=str(_LOGO_PATH),
                                  style={"width": "44px", "height": "44px",
                                         "margin-right": "12px"})
                Label(text="Тендер-ассистент",
                      style={"color": _WHITE, "font-size": "20px",
                             "font-weight": "bold"})

            # ── Исходные материалы ────────────────────────────────────────

            Label(text="ИСХОДНЫЕ МАТЕРИАЛЫ",
                  style={**label_s(), "font-size": "11px",
                         "font-weight": "bold", "margin-bottom": "8px"})

            _picker_row("Требования тендера", requirements_file, "Не выбраны",
                        pick_requirements,
                        "Документ, из которого берётся перечень документов")
            _picker_row("Папка с документами организации", archive_folder,
                        "Не выбрана", pick_archive,
                        "Архив, где ищутся файлы для комплекта заявки")
            _picker_row("Шаблон заявки", template_file, "Не выбран", pick_template,
                        "Форма, которую нужно заполнить")

            # ── Базы знаний ───────────────────────────────────────────────

            Label(text="БАЗЫ ЗНАНИЙ (НЕОБЯЗАТЕЛЬНО)",
                  style={**label_s(), "font-size": "11px", "font-weight": "bold",
                         "margin-top": "8px", "margin-bottom": "8px"})

            _picker_row("Нормативная база", normative_folder, "Не выбрана",
                        pick_normative,
                        "По ней проверяется перечень документов")
            _picker_row("База знаний и эталонные заявки", knowledge_folder,
                        "Не выбрана", pick_knowledge,
                        "Из неё берутся значения для заполнения заявки")

            # ── Результаты ────────────────────────────────────────────────

            Label(text="РЕЗУЛЬТАТ",
                  style={**label_s(), "font-size": "11px", "font-weight": "bold",
                         "margin-top": "8px", "margin-bottom": "8px"})

            _picker_row("Папка для результатов", results_folder, "Не выбрана",
                        pick_results, "Сюда попадут комплект документов и отчёты")

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


def _results_view(result: dict):
    """Сводка по трём этапам плюс кнопки открытия файлов."""
    document_list = result.get("document_list") or {}
    prepared = result.get("prepared_documents") or {}
    application = result.get("application") or {}

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
        _stat_row("Требуется документов:", str(len(documents)))
        if excluded:
            _stat_row("Исключено по нормативной базе:", str(len(excluded)), _MUTED)
        if document_list.get("report_path"):
            Button(title="Открыть отчёт",
                   on_click=lambda _=None: _open_in_explorer(
                       document_list["report_path"]),
                   style={**btn(), "margin-top": "8px"})

    # Этап 2
    with VBoxView(style=card()):
        Label(text="2. Подготовка документов",
              style={**value_s(), "font-weight": "bold", "margin-bottom": "8px"})
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
    logger.info(f"Запуск интерфейса, API: {API_URL}")

    app = edifice.App(TenderAssistantApp())
    _apply_theme(QApplication.instance())
    app.start()
