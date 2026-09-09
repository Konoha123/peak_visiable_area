import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """让应用自有 logger 的 INFO 输出可见（uvicorn 仅配置其命名空间日志）。"""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    logging.getLogger("app").setLevel(logging.INFO)


def create_app() -> FastAPI:
    app = FastAPI(title="Peak Visible Area")
    _configure_logging()

    app.include_router(router)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
