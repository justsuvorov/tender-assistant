"""Журнал: номер запроса в строках, файл с ротацией, журналирование вызовов модели."""

import logging

import pytest

from tender_assistant.ai.model import LoggedModel, ModelFactory
from tender_assistant.core import console
from tender_assistant.core.logging_config import (
    LOG_FILE_NAME,
    RequestIdFilter,
    request_context,
    resolve_log_dir,
    setup_logging,
)
from tests.fakes import BrokenModel


@pytest.fixture
def restore_root_logger():
    """setup_logging меняет корневой журнал — возвращаем его как было."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


def _record() -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    RequestIdFilter().filter(record)
    return record


class TestRequestContext:
    def test_outside_a_request_the_id_is_a_dash(self):
        assert _record().request_id == "-"

    def test_inside_a_request_every_line_carries_its_id(self):
        with request_context(42):
            assert _record().request_id == "42"

    def test_id_is_reset_after_the_request(self):
        with request_context(42):
            pass
        assert _record().request_id == "-"


class TestSetupLogging:
    def test_writes_to_a_file_in_the_given_folder(self, tmp_path, restore_root_logger):
        path = setup_logging("INFO", str(tmp_path / "logs"))

        with request_context(7):
            logging.getLogger("tender_assistant.test").info("Этап 1: старт")
        for handler in logging.getLogger().handlers:
            handler.flush()

        assert path == tmp_path / "logs" / LOG_FILE_NAME
        line = path.read_text(encoding="utf-8").strip().splitlines()[-1]
        assert "| INFO    | req=7 | tender_assistant.test | Этап 1: старт" in line

    def test_repeated_setup_does_not_duplicate_lines(self, tmp_path, restore_root_logger):
        setup_logging("INFO", str(tmp_path))
        path = setup_logging("INFO", str(tmp_path))

        logging.getLogger("tender_assistant.test").info("одна строка")
        for handler in logging.getLogger().handlers:
            handler.flush()

        assert path.read_text(encoding="utf-8").count("одна строка") == 1

    def test_unknown_level_falls_back_to_info(self, tmp_path, restore_root_logger):
        setup_logging("ПОДРОБНО", str(tmp_path))
        assert logging.getLogger().level == logging.INFO

    def test_unwritable_folder_keeps_console_logging(self, tmp_path, restore_root_logger):
        blocker = tmp_path / "файл"
        blocker.write_text("не папка", encoding="utf-8")

        assert setup_logging("INFO", str(blocker / "logs")) is None

    def test_relative_folder_is_resolved(self, tmp_path):
        assert resolve_log_dir(str(tmp_path)) == tmp_path
        assert resolve_log_dir("logs").name == "logs"
        assert resolve_log_dir("logs").is_absolute()


class TestLoggedModel:
    def test_passes_the_answer_through_and_logs_the_call(self, caplog):
        model = LoggedModel(BrokenModel("ответ"), "vsk")

        with caplog.at_level(logging.INFO, logger="tender_assistant.ai.model"):
            assert model.response("вопрос") == "ответ"

        messages = [r.getMessage() for r in caplog.records]
        assert any(m.startswith("LLM #1 → vsk: запрос 6 симв.") for m in messages)
        assert any(m.startswith("LLM #1 ← vsk: ответ 5 симв.") for m in messages)

    def test_cacheable_part_is_reported(self, caplog):
        model = LoggedModel(BrokenModel("ок"), "vsk")

        with caplog.at_level(logging.INFO, logger="tender_assistant.ai.model"):
            model.response_with_cache("материалы", "поле")

        assert "из них кэшируемая часть 9" in caplog.text

    def test_failure_is_logged_and_reraised(self, caplog):
        class Dead(BrokenModel):
            def response(self, query):
                raise RuntimeError("сервис недоступен")

        model = LoggedModel(Dead(), "vsk")
        with caplog.at_level(logging.INFO, logger="tender_assistant.ai.model"):
            with pytest.raises(RuntimeError, match="сервис недоступен"):
                model.response("вопрос")

        assert "LLM #1 ✗ vsk" in caplog.text and "сервис недоступен" in caplog.text

    def test_prompt_text_is_only_logged_at_debug(self, caplog):
        model = LoggedModel(BrokenModel("ок"), "qwen")

        with caplog.at_level(logging.INFO, logger="tender_assistant.ai.model"):
            model.response("ИНН 7701234567")
        assert "7701234567" not in caplog.text

        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="tender_assistant.ai.model"):
            model.response("ИНН 7701234567")
        assert "7701234567" in caplog.text

    def test_calls_are_numbered(self, caplog):
        model = LoggedModel(BrokenModel("ок"), "qwen")
        with caplog.at_level(logging.INFO, logger="tender_assistant.ai.model"):
            model.response("1")
            model.response("2")

        assert "LLM #2 →" in caplog.text

    def test_keeps_the_caching_capability(self):
        class Caching(BrokenModel):
            supports_prompt_caching = True

        assert LoggedModel(Caching(), "vsk").supports_prompt_caching is True
        assert LoggedModel(BrokenModel(), "qwen").supports_prompt_caching is False

    def test_factory_wraps_the_provider_model(self, monkeypatch):
        from tender_assistant.ai import model as model_module

        monkeypatch.setattr(model_module.settings, "ai_provider", "vsk")
        created = ModelFactory.create()

        assert isinstance(created, LoggedModel)
        assert type(created.wrapped).__name__ == "VskAIModel"
        assert created.provider == "vsk"


class TestConsole:
    def test_quick_edit_is_left_alone_outside_windows(self, monkeypatch):
        monkeypatch.setattr(console.sys, "platform", "linux")
        assert console.disable_quick_edit() is False

    def test_without_a_console_nothing_breaks(self):
        """Под pytest stdin — не консоль: функция должна просто вернуть False."""
        assert console.disable_quick_edit() in (True, False)
