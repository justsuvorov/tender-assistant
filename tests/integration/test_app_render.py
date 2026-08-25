"""Отрисовка интерфейса вне экрана.

Ловит то, что не поймать разбором кода: ошибки в теле render — неверные props,
несуществующие элементы, падения на вложенных представлениях. Всё рисуется
за один запуск цикла событий: поднимать его несколько раз в одном процессе
ненадёжно.
"""

import os
import traceback

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="интерфейс не установлен")
pytest.importorskip("edifice", reason="интерфейс не установлен")

import edifice  # noqa: E402
from edifice import VScrollView, Window  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.main import TenderAssistantApp, _results_view  # noqa: E402

pytestmark = pytest.mark.integration


SAMPLE_RESPONSE = {
    "document_list": {
        "documents": [{"name": "Устав"}, {"name": "Выписка из ЕГРЮЛ"}],
        "excluded": [{"name": "Паспорт ИП"}],
        "report_path": r"D:\r\document_list.md",
    },
    "prepared_documents": {
        "rows": [
            {"name": "Устав", "status": "Есть", "files": ["ustav.docx"]},
            {"name": "Выписка из ЕГРЮЛ", "status": "Проверить", "files": ["v.pdf"]},
            {"name": "Справка", "status": "Нет", "files": []},
        ],
        "result_folder": r"D:\r\komplekt",
        "report_path": r"D:\r\documents_status.md",
    },
    "application": {
        "fields": [
            {"label": "ИНН", "status": "found"},
            {"label": "Юридический адрес", "status": "check"},
            {"label": "Контактный телефон", "status": "missing"},
        ],
        "filled_path": r"D:\r\form_заполнено.docx",
        "report_path": r"D:\r\application_fill.md",
    },
}


@edifice.component
def _Harness(self):
    """Пустая форма и блок результатов рядом — обе ветки отрисовки сразу."""
    with Window(title="Проверка отрисовки"):
        with VScrollView():
            TenderAssistantApp()
            _results_view(SAMPLE_RESPONSE)


@pytest.fixture(scope="module")
def rendered():
    qapp = QApplication.instance() or QApplication([])

    failures = []
    original_hook = __import__("sys").excepthook

    def collect(exc_type, exc, tb):
        failures.append("".join(traceback.format_exception(exc_type, exc, tb)))

    __import__("sys").excepthook = collect
    QTimer.singleShot(3000, qapp.quit)

    try:
        edifice.App(
            _Harness(), create_application=False, qapplication=qapp
        ).start()
    except Exception:
        failures.append(traceback.format_exc())
    finally:
        __import__("sys").excepthook = original_hook

    texts = {
        widget.text()
        for widget in qapp.allWidgets()
        if hasattr(widget, "text") and callable(widget.text) and widget.text()
    }
    return {"texts": texts, "failures": failures}


def test_renders_without_errors(rendered):
    assert rendered["failures"] == [], "\n".join(rendered["failures"])


@pytest.mark.parametrize("caption", [
    "Требования тендера",
    "Папка с документами организации",
    "Шаблон заявки",
    "Нормативная база",
    "База знаний и эталонные заявки",
    "Папка для результатов",
])
def test_every_input_has_a_picker(rendered, caption):
    assert caption in rendered["texts"]


def test_submit_button_is_present(rendered):
    assert "Подготовить заявку" in rendered["texts"]


@pytest.mark.parametrize("caption", [
    "1. Перечень документов",
    "2. Подготовка документов",
    "3. Заполнение заявки",
])
def test_all_three_stages_are_summarised(rendered, caption):
    assert caption in rendered["texts"]


@pytest.mark.parametrize("caption", [
    "Открыть комплект", "Открыть заявку", "Открыть отчёт",
])
def test_result_actions_are_offered(rendered, caption):
    assert caption in rendered["texts"]
