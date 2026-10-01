import logging
import time

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from tender_assistant.core.config import settings
from tender_assistant.core.logging_config import request_context, setup_logging
from tender_assistant.models.request import APIRequest
from tender_assistant.services.assistant import TenderAssistantService

# При импорте, а не в __main__: сервис запускают и как EXE, и через
# `uvicorn main:app`, и gunicorn'ом в Docker — журнал нужен во всех случаях.
_LOG_PATH = setup_logging(settings.log_level, settings.log_dir)

logger = logging.getLogger("tender_assistant.api")

app = FastAPI(
    title="Tender Assistant",
    description=(
        "Ассистент подготовки конкурсной заявки: определяет перечень необходимых "
        "документов, собирает их из архива и заполняет шаблон заявки."
    ),
)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/update")
def submit(request: APIRequest):
    """Полный цикл: перечень документов → сбор файлов → заполнение заявки."""
    with request_context(request.message_id):
        logger.info("Получен запрос на подготовку заявки")
        started = time.perf_counter()
        try:
            result = TenderAssistantService(request=request).result()
        except (ValueError, FileNotFoundError) as exc:
            logger.warning("Ответ 400 — ошибка во входных данных: %s", exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.error("Ответ 502 — сбой модели: %s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Ответ 500 — непредвиденная ошибка")
            raise HTTPException(
                status_code=500, detail=f"Внутренняя ошибка сервиса: {exc}"
            ) from exc

        logger.info("Ответ 200 за %.1f с", time.perf_counter() - started)
        return JSONResponse(content=jsonable_encoder(result))


if __name__ == "__main__":
    import uvicorn

    from tender_assistant.core.console import disable_quick_edit

    # Щелчок мышью по консоли иначе останавливает сервис до нажатия Esc.
    disable_quick_edit()
    logger.info(
        "Запуск сервиса: провайдер %s, журнал %s",
        settings.ai_provider, _LOG_PATH or "только консоль",
    )

    # Объект app передаётся напрямую, а не строкой "main:app": reload=True
    # запускает отдельный процесс-наблюдатель по пути к исходнику main.py —
    # в собранном PyInstaller-бандле такого файла на диске нет, и запуск
    # ломается. Для разработки с автоперезагрузкой используйте
    # `uvicorn main:app --reload` из командной строки (см. README).
    #
    # log_config=None: uvicorn не перенастраивает журнал под себя, и его
    # строки (запуск, обращения к API) идут в тот же формат и тот же файл.
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1, log_config=None)
