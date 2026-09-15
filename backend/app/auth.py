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
    """Garantisce che l'admin ADMIN_EMAIL esista con password ADMIN_PASSWORD.

    Se ADMIN_PASSWORD e' impostata in ambiente/.env, la password dell'utente
    ADMIN_EMAIL viene allineata ad essa ad OGNI avvio (non solo al primo):
    per "resettare" l'admin basta cambiare ADMIN_PASSWORD in .env e
    riavviare, senza mai dover cancellare il database. Se ADMIN_EMAIL non
    esiste ancora, viene creato ora (che sia il primissimo avvio, o un
    avvio successivo con una nuova email scelta in .env: non tocca/duplica
    admin creati in run precedenti con altra email).
    """
    db = SessionLocal()
    try:
        email = ADMIN_EMAIL.strip().lower()
        existing = db.query(User).filter_by(email=email).first()

        if existing is not None:
            if ADMIN_PASSWORD:
                existing.password_hash = hash_password(ADMIN_PASSWORD)
                existing.is_admin = True
                db.commit()
                print("=" * 72)
                print(f"Admin sincronizzato da .env — email: {email}  password: quella in ADMIN_PASSWORD")
                print("=" * 72)
            return

        password = ADMIN_PASSWORD
        generated = password is None
        if generated:
            password = secrets.token_urlsafe(9)
        user = User(name="Amministratore", email=email, password_hash=hash_password(password), is_admin=True)
        db.add(user)
        db.commit()
        print("=" * 72)
        if generated:
            print(f"Admin iniziale creato — email: {ADMIN_EMAIL}  password: {password}")
            print("Imposta ADMIN_EMAIL/ADMIN_PASSWORD in backend/.env per fissarle e poterle 'resettare' cambiandole li'.")
        else:
            print(f"Admin creato da .env — email: {email}  password: quella in ADMIN_PASSWORD")
        print("=" * 72)
    finally:
        db.close()
