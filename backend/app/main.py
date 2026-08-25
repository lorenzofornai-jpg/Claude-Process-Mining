from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import init_db
from app.routers import ingestion

APP_DIR = Path(__file__).resolve().parent

app = FastAPI(title="AI Process Mining - Modulo 1: Ingestion (prototipo)")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
app.include_router(ingestion.router)


@app.on_event("startup")
def _startup() -> None:
    init_db()
