from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import current_user, verify_password
from app.config import STATIC_VERSION
from app.db import SessionLocal
from app.models import User

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals["static_version"] = STATIC_VERSION


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if current_user(request) is not None:
        return RedirectResponse("/ingestion/dashboard", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(email=email.strip().lower()).first()
    finally:
        db.close()

    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Email o password non corretti."},
            status_code=401,
        )

    request.session["user_id"] = user.id
    return RedirectResponse("/ingestion/dashboard", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    # NON aggiungere qui un secondo Set-Cookie esplicito (es. response.delete_cookie):
    # SessionMiddleware ne manda gia' uno da solo quando la sessione diventa vuota, e
    # avere DUE Set-Cookie per lo stesso nome cookie in una risposta e' un caso limite
    # che un proxy che traduce HTTP/2<->HTTP/1.1 (come l'inoltro porte di Codespaces)
    # puo' gestire in modo incoerente - bug reale visto in test: il logout tornava a
    # "I miei processi" in modo incostante invece che al login, perche' il cookie non
    # veniva davvero cancellato lato client prima della richiesta successiva.
    return RedirectResponse("/login", status_code=303)
