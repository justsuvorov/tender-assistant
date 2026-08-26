FROM tiangolo/uvicorn-gunicorn-fastapi:python3.11

# Зависимости отдельным слоем: пересобираются только при правке requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Образ сам выбирает модуль запуска и проверяет /app/app/main.py ПЕРВЫМ.
# В этом проекте app/main.py — настольный интерфейс (edifice/PySide6), а не API,
# поэтому модуль задан явно: main:app из корневого main.py. Каталог app/ ещё и
# исключён в .dockerignore, так что в образ не попадают тяжёлые GUI-зависимости.
ENV MODULE_NAME=main \
    VARIABLE_NAME=app
