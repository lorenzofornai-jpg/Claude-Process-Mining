from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import seed_default_admin
from app.config import SESSION_SECRET_KEY
from app.db import init_db
from app.routers import admin, analysis, ingestion, login

APP_DIR = Path(__file__).resolve().parent

app = FastAPI(title="AI Process Mining - Modulo 1: Ingestion (prototipo)")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY, same_site="lax")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
app.include_router(login.router)
app.include_router(admin.router)
app.include_router(ingestion.router)
app.include_router(analysis.router)


@app.middleware("http")
async def _no_store_dynamic_pages(request, call_next):
    """Le pagine dinamiche (tutto tranne /static/*) non vanno mai in cache:
    un browser che tenesse in cache una pagina autenticata potrebbe
    rimostrarla dopo il logout invece di richiedere davvero /login al
    server (bug reale visto in test: "Esci" sembrava non funzionare, pur
    essendo il cookie di sessione cancellato correttamente lato server)."""
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        # no-store da solo basta per i browser moderni, ma un proxy/CDN
        # intermedio (es. l'inoltro porte di Codespaces) potrebbe rispettare
        # solo le direttive piu' vecchie: aggiunte tutte per non lasciare
        # scappatoie a nessun livello della catena.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.on_event("startup")
def _startup() -> None:
    init_db()
    seed_default_admin()
