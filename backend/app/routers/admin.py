from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import current_user, hash_password
from app.db import SessionLocal
from app.models import ProcessAssignment, ProcessWorkspace, User

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _require_admin(request: Request):
    """Ritorna (user, None) se autorizzato, altrimenti (None, redirect_o_403)."""
    user = current_user(request)
    if user is None:
        return None, RedirectResponse("/login", status_code=303)
    if not user.is_admin:
        return None, HTMLResponse(
            "Accesso negato: questa pagina è riservata agli amministratori.", status_code=403
        )
    return user, None


@router.get("", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    user, denied = _require_admin(request)
    if denied:
        return denied

    db = SessionLocal()
    try:
        workspaces = db.query(ProcessWorkspace).order_by(ProcessWorkspace.created_at.desc()).all()
        assignments = {
            a.workspace_id: a
            for a in db.query(ProcessAssignment).filter_by(role="data_engineer").all()
        }
        users_by_id = {u.id: u for u in db.query(User).all()}
        rows = [
            {"workspace": ws, "assigned_user": users_by_id.get(assignments[ws.id].user_id) if ws.id in assignments else None}
            for ws in workspaces
        ]
        data_engineers = [u for u in users_by_id.values() if not u.is_admin]
    finally:
        db.close()

    return templates.TemplateResponse(
        "admin_dashboard.html",
        {"request": request, "user": user, "rows": rows, "data_engineers": data_engineers},
    )


@router.get("/users/new", response_class=HTMLResponse)
def new_user_form(request: Request):
    user, denied = _require_admin(request)
    if denied:
        return denied
    return templates.TemplateResponse("admin_new_user.html", {"request": request, "user": user, "error": None})


@router.post("/users/new")
def create_user(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...)):
    user, denied = _require_admin(request)
    if denied:
        return denied

    email_norm = email.strip().lower()
    db = SessionLocal()
    try:
        if db.query(User).filter_by(email=email_norm).first():
            return templates.TemplateResponse(
                "admin_new_user.html",
                {"request": request, "user": user, "error": f"Esiste già un utente con email {email_norm}."},
                status_code=400,
            )
        db.add(User(name=name, email=email_norm, password_hash=hash_password(password), is_admin=False))
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/admin", status_code=303)


@router.post("/processes/{workspace_id}/assign")
def assign_data_engineer(request: Request, workspace_id: str, user_id: str = Form(...)):
    user, denied = _require_admin(request)
    if denied:
        return denied

    db = SessionLocal()
    try:
        existing = db.query(ProcessAssignment).filter_by(workspace_id=workspace_id, role="data_engineer").first()
        if existing:
            existing.user_id = user_id
            existing.assigned_by = user.name
        else:
            db.add(ProcessAssignment(
                workspace_id=workspace_id, user_id=user_id, role="data_engineer", assigned_by=user.name,
            ))
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/admin", status_code=303)
