from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import current_user, hash_password
from app.config import STATIC_VERSION
from app.db import SessionLocal
from app.models import ProcessAssignment, ProcessWorkspace, User

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals["static_version"] = STATIC_VERSION


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


ASSIGNABLE_ROLES = ["data_engineer", "data_analyst"]
ROLE_LABELS = {"data_engineer": "Data Engineer", "data_analyst": "Data Analyst"}


@router.get("", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    user, denied = _require_admin(request)
    if denied:
        return denied

    db = SessionLocal()
    try:
        workspaces = db.query(ProcessWorkspace).order_by(ProcessWorkspace.created_at.desc()).all()
        users_by_id = {u.id: u for u in db.query(User).all()}
        # una entry per ogni (workspace, ruolo) assegnato: uno stesso processo puo'
        # avere sia un Data Engineer sia un Data Analyst assegnati, righe distinte
        assigned_user_by_workspace_role = {
            (a.workspace_id, a.role): users_by_id.get(a.user_id)
            for a in db.query(ProcessAssignment).filter(ProcessAssignment.role.in_(ASSIGNABLE_ROLES)).all()
        }
        rows = [
            {
                "workspace": ws,
                "assignments": {
                    role: assigned_user_by_workspace_role.get((ws.id, role))
                    for role in ASSIGNABLE_ROLES
                },
            }
            for ws in workspaces
        ]
        assignable_users = [u for u in users_by_id.values() if not u.is_admin]
    finally:
        db.close()

    return templates.TemplateResponse(
        "admin_dashboard.html",
        {
            "request": request, "user": user, "rows": rows, "assignable_users": assignable_users,
            "assignable_roles": ASSIGNABLE_ROLES, "role_labels": ROLE_LABELS,
        },
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
def assign_role(request: Request, workspace_id: str, user_id: str = Form(...), role: str = Form(...)):
    user, denied = _require_admin(request)
    if denied:
        return denied
    if role not in ASSIGNABLE_ROLES:
        return HTMLResponse(f"Ruolo non valido: {role}", status_code=400)

    db = SessionLocal()
    try:
        existing = db.query(ProcessAssignment).filter_by(workspace_id=workspace_id, role=role).first()
        if existing:
            existing.user_id = user_id
            existing.assigned_by = user.name
        else:
            db.add(ProcessAssignment(
                workspace_id=workspace_id, user_id=user_id, role=role, assigned_by=user.name,
            ))
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/admin", status_code=303)
