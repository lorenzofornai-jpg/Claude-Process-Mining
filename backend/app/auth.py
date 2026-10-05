"""Autenticazione e controllo accessi.

Sessione via cookie firmato (Starlette SessionMiddleware), password con
bcrypt. Niente framework di auth: le route chiamano esplicitamente
`current_user(request)` e i controlli di ruolo, nello stesso stile
manuale gia' usato nel resto del router di ingestion.
"""
from __future__ import annotations

import time

import bcrypt

from app.config import BOOTSTRAP_ADMIN_EMAIL, BOOTSTRAP_ADMIN_PASSWORD, SESSION_IDLE_MINUTES
from app.db import SessionLocal
from app.models import ProcessAssignment, User


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def start_session(request, user: User) -> None:
    lang = request.session.get("lang")  # la lingua scelta prima di accedere resta
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["last_seen"] = int(time.time())
    if user.language or lang:
        request.session["lang"] = user.language or lang


def current_user(request) -> User | None:
    """Utente loggato, o None. Se la sessione e' inattiva da piu' di
    SESSION_IDLE_MINUTES la svuota (-> login con avviso "sessione scaduta"),
    altrimenti rinnova il timestamp di ultima attivita'."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    now = int(time.time())
    last_seen = request.session.get("last_seen", 0)
    if now - last_seen > SESSION_IDLE_MINUTES * 60:
        request.session.clear()
        request.session["expired"] = True
        return None
    request.session["last_seen"] = now
    db = SessionLocal()
    try:
        return db.get(User, user_id)
    finally:
        db.close()


def has_process_access(user: User, workspace_id: str, required_role: str = "data_engineer") -> bool:
    """True se l'utente puo' lavorare su un dato modulo per questo processo:
    solo un utente con quel ruolo specifico assegnato a questo workspace.
    L'admin NON ha accesso ai moduli: gestisce utenti, processi e
    assegnazioni da Amministrazione, ma non crea strutture ne' analisi.
    Uno stesso utente puo' avere piu' ruoli, anche sullo stesso processo (es.
    data_engineer per il Modulo 1 e data_analyst per il Modulo 2): sono righe
    distinte in ProcessAssignment, non un attributo fisso sull'utente."""
    if user.is_admin:
        return False
    db = SessionLocal()
    try:
        return (
            db.query(ProcessAssignment)
            .filter_by(workspace_id=workspace_id, user_id=user.id, role=required_role)
            .first()
            is not None
        )
    finally:
        db.close()


def seed_default_admin() -> None:
    """Se non esiste ancora NESSUN amministratore nel database, crea l'unico
    utente di bootstrap superuser/superuser.

    Deliberatamente ignora qualunque variabile d'ambiente: la sola fonte di
    verita' su chi e' amministratore e' il database, mai l'.env (bug reale
    visto in test - un ADMIN_EMAIL rimasto impostato in .env da una sessione
    precedente creava un secondo admin "fantasma" accanto a superuser invece
    di sostituirlo). Se un admin esiste gia' (bootstrap o creato a mano da
    "Amministrazione"), questa funzione non fa nulla: niente sync automatico
    della password ad ogni avvio come prima - per cambiarla si passa
    dall'app, creando un nuovo admin o (quando ci sara') da un cambio
    password self-service.
    """
    db = SessionLocal()
    try:
        if db.query(User).filter_by(is_admin=True).first() is not None:
            return

        email = BOOTSTRAP_ADMIN_EMAIL.strip().lower()
        user = User(
            name="Amministratore", email=email,
            password_hash=hash_password(BOOTSTRAP_ADMIN_PASSWORD), is_admin=True,
        )
        db.add(user)
        db.commit()
        print("=" * 72)
        print(f"Admin di bootstrap creato — utente: {email}  password: {BOOTSTRAP_ADMIN_PASSWORD}")
        print("Crea un admin vero da Amministrazione appena entri.")
        print("=" * 72)
    finally:
        db.close()
