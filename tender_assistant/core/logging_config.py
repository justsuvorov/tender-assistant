"""Журнал сервиса: консоль + файл с ротацией, у каждой строки — номер запроса.

Один прогон пайплайна — это десятки вызовов модели в течение многих минут.
Чтобы по журналу можно было восстановить, что происходило с конкретной
заявкой, каждая строка помечается ``req=<message_id>`` запроса, внутри
которого она записана (см. ``request_context``). Строки вне запроса —
запуск сервиса, служебные сообщения — помечаются ``req=-``.
"""

import contextvars
import logging
import sys
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator, Optional

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | req=%(request_id)s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE_NAME = "tender_assistant.log"

# 10 файлов по 10 МБ: хватает на недели работы, а диск не переполнится.
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 10

# Библиотеки, которые на INFO пишут строку на каждый HTTP-запрос к модели.
# Наш LoggedModel уже пишет по строке на вызов — их INFO только дублирует.
_NOISY_LOGGERS = ("httpx", "httpcore", "anthropic", "google_genai", "urllib3")

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

# Маркер на наших обработчиках: повторный setup_logging() (перезагрузка
# модуля, тесты) не должен дублировать каждую строку журнала.
_HANDLER_MARK = "_tender_assistant_handler"


class RequestIdFilter(logging.Filter):
    """Подставляет в запись номер текущего запроса (поле %(request_id)s)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


@contextmanager
def request_context(request_id) -> Iterator[None]:
    """Все записи журнала внутри блока помечаются этим номером запроса."""
    token = _request_id.set(str(request_id))
    try:
        yield
    finally:
        _request_id.reset(token)


def resolve_log_dir(configured: str) -> Path:
    """Относительный путь — от папки с EXE (собранный сервис) или от текущей.

    У собранного EXE текущая папка зависит от того, как его запустили
    (ярлык, планировщик, run-all.bat), а журнал должен лежать рядом с ним.
    """
    path = Path(configured)
    if path.is_absolute():
        return path
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    return base / path


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> Optional[Path]:
    """Настраивает корневой журнал. Возвращает путь к файлу журнала.

    Если папку журнала создать нельзя (нет прав), сервис продолжает работать
    с журналом только в консоли — потерять запуск из-за журнала хуже.
    """
    root = logging.getLogger()
    numeric_level = logging.getLevelName(str(level).upper())
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO
    root.setLevel(numeric_level)

    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    request_filter = RequestIdFilter()

    console = logging.StreamHandler(sys.stderr)
    _attach(root, console, formatter, request_filter)

    log_path: Optional[Path] = None
    try:
        directory = resolve_log_dir(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / LOG_FILE_NAME
        file_handler = RotatingFileHandler(
            log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        _attach(root, file_handler, formatter, request_filter)
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Журнал не будет записан в файл (%s): %s", log_dir, exc
        )
        log_path = None

    noisy_level = logging.DEBUG if numeric_level <= logging.DEBUG else logging.WARNING
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(noisy_level)

    return log_path


def _attach(root, handler, formatter, request_filter) -> None:
    handler.setFormatter(formatter)
    handler.addFilter(request_filter)
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)
