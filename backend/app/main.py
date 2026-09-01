from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import seed_default_admin
from app.config import SESSION_SECRET_KEY
from app.db import init_db
from app.routers import admin, ingestion, login

APP_DIR = Path(__file__).resolve().parent

app = FastAPI(title="AI Process Mining - Modulo 1: Ingestion (prototipo)")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY, same_site="lax")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
app.include_router(login.router)
app.include_router(admin.router)
app.include_router(ingestion.router)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    seed_default_admin()
