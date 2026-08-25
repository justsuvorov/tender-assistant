"""Общая палитра для отчётов и заполнения шаблонов заявки."""

GREEN = "D6F0D6"
RED = "F0D6D6"
YELLOW = "FFF3CD"
HEADER = "1F4E79"  # тёмно-синий фон шапки таблицы


def status_fill(status: str, fill_map: dict[str, str]) -> str:
    return fill_map.get(status.lower().strip(), "FFFFFF")


# Статусы заполнения полей заявки (этап 3)
FIELD_STATUS_FILL = {
    "found": GREEN,     # найдено и подтверждено
    "check": YELLOW,    # найдено, но требует проверки
    "missing": RED,     # информации нет
}

# Статусы подготовки документов (этап 2)
DOCUMENT_STATUS_FILL = {
    "есть": GREEN,
    "проверить": YELLOW,
    "нет": RED,
}


def field_fill(status: str) -> str:
    return status_fill(status, FIELD_STATUS_FILL)


def document_fill(status: str) -> str:
    return status_fill(status, DOCUMENT_STATUS_FILL)
