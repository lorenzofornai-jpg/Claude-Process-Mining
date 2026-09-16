"""Autenticazione e controllo accessi.

Sessione via cookie firmato (Starlette SessionMiddleware), password con
bcrypt. Niente framework di auth: le route chiamano esplicitamente
`current_user(request)` e i controlli di ruolo, nello stesso stile
manuale gia' usato nel resto del router di ingestion.
"""
from __future__ import annotations

import bcrypt

from app.config import ADMIN_CREDENTIALS_FROM_ENV, ADMIN_EMAIL, ADMIN_PASSWORD
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


def has_process_access(user: User, workspace_id: str, required_role: str = "data_engineer") -> bool:
    """True se l'utente puo' lavorare su un dato modulo per questo processo:
    admin (accesso illimitato a tutto), oppure un utente con quel ruolo
    specifico assegnato a questo workspace. Uno stesso utente puo' avere piu'
    ruoli, anche sullo stesso processo (es. data_engineer per il Modulo 1 e
    data_analyst per il Modulo 2): sono righe distinte in ProcessAssignment,
    non un attributo fisso sull'utente."""
    if user.is_admin:
        return True
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
    """Garantisce che l'admin ADMIN_EMAIL esista con password ADMIN_PASSWORD.

    Di default (nessun .env): "superuser"/"superuser" - le uniche credenziali
    che esistono al primissimo avvio dell'app per un nuovo cliente, pensate
    per essere sostituite subito creando admin/utenti veri da li'.

    La password dell'utente ADMIN_EMAIL viene allineata ad ADMIN_PASSWORD ad
    OGNI avvio (non solo al primo): per "resettare" l'admin (o cambiare le
    credenziali di bootstrap) basta impostare ADMIN_EMAIL/ADMIN_PASSWORD in
    backend/.env e riavviare, senza mai dover cancellare il database. Se
    ADMIN_EMAIL non esiste ancora, viene creato ora (che sia il primissimo
    avvio, o un avvio successivo con una nuova email scelta in .env: non
    tocca/duplica admin creati in run precedenti con altra email - inclusi
    quelli creati a mano dall'admin via "Amministrazione", che restano
    intatti perche' hanno un'email diversa da ADMIN_EMAIL).
    """
    db = SessionLocal()
    try:
        email = ADMIN_EMAIL.strip().lower()
        existing = db.query(User).filter_by(email=email).first()
        source = "da .env" if ADMIN_CREDENTIALS_FROM_ENV else "default (nessun .env)"

        if existing is not None:
            existing.password_hash = hash_password(ADMIN_PASSWORD)
            existing.is_admin = True
            db.commit()
            print("=" * 72)
            print(f"Admin sincronizzato ({source}) — email: {email}  password: quella in ADMIN_PASSWORD")
            print("=" * 72)
            return

        user = User(name="Amministratore", email=email, password_hash=hash_password(ADMIN_PASSWORD), is_admin=True)
        db.add(user)
        db.commit()
        print("=" * 72)
        print(f"Admin iniziale creato ({source}) — email: {email}  password: quella in ADMIN_PASSWORD")
        if not ADMIN_CREDENTIALS_FROM_ENV:
            print("Sta usando le credenziali di bootstrap superuser/superuser: crea un admin vero da")
            print("Amministrazione appena entri, oppure fissa ADMIN_EMAIL/ADMIN_PASSWORD in backend/.env.")
        print("=" * 72)
    finally:
        db.close()
