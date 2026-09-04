from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from tender_assistant.models.request import APIRequest
from tender_assistant.services.assistant import TenderAssistantService

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
    try:
        result = TenderAssistantService(request=request).result()
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return JSONResponse(content=jsonable_encoder(result))


if __name__ == "__main__":
    import uvicorn

    # Объект app передаётся напрямую, а не строкой "main:app": reload=True
    # запускает отдельный процесс-наблюдатель по пути к исходнику main.py —
    # в собранном PyInstaller-бандле такого файла на диске нет, и запуск
    # ломается. Для разработки с автоперезагрузкой используйте
    # `uvicorn main:app --reload` из командной строки (см. README).
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1, log_level="info")
