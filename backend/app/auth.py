"""Autenticazione e controllo accessi.

Sessione via cookie firmato (Starlette SessionMiddleware), password con
bcrypt. Niente framework di auth: le route chiamano esplicitamente
`current_user(request)` e i controlli di ruolo, nello stesso stile
manuale gia' usato nel resto del router di ingestion.
"""
from __future__ import annotations

import secrets

import bcrypt

from app.config import ADMIN_EMAIL, ADMIN_PASSWORD
from app.db import SessionLocal
from app.models import ProcessAssignment, User


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def current_user(request) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    db = SessionLocal()
    try:
        return db.get(User, user_id)
    finally:
        db.close()


def has_process_access(user: User, workspace_id: str) -> bool:
    """True se l'utente puo' lavorare sul Modulo 1 di questo processo:
    admin, oppure Data Engineer assegnato a questo specifico workspace."""
    if user.is_admin:
        return True
    db = SessionLocal()
    try:
        return (
            db.query(ProcessAssignment)
            .filter_by(workspace_id=workspace_id, user_id=user.id, role="data_engineer")
            .first()
            is not None
        )
    finally:
        db.close()


def seed_default_admin() -> None:
    """Crea l'admin iniziale al primo avvio, se non ne esiste gia' uno."""
    db = SessionLocal()
    try:
        if db.query(User).filter_by(is_admin=True).first():
            return
        password = ADMIN_PASSWORD
        generated = password is None
        if generated:
            password = secrets.token_urlsafe(9)
        user = User(
            name="Amministratore",
            email=ADMIN_EMAIL.strip().lower(),
            password_hash=hash_password(password),
            is_admin=True,
        )
        db.add(user)
        db.commit()
        if generated:
            print("=" * 72)
            print(f"Admin iniziale creato — email: {ADMIN_EMAIL}  password: {password}")
            print("Imposta ADMIN_EMAIL/ADMIN_PASSWORD in backend/.env per personalizzare.")
            print("=" * 72)
    finally:
        db.close()
