"""Autenticazione e controllo accessi.

Sessione via cookie firmato (Starlette SessionMiddleware), password con
bcrypt. Niente framework di auth: le route chiamano esplicitamente
`current_user(request)` e i controlli di ruolo, nello stesso stile
manuale gia' usato nel resto del router di ingestion.
"""
from __future__ import annotations

import secrets
import threading
import time

import bcrypt

from app.config import BOOTSTRAP_ADMIN_EMAIL, BOOTSTRAP_ADMIN_PASSWORD, DATA_DIR, SESSION_IDLE_MINUTES
from app.db import SessionLocal
from app.models import ProcessAssignment, User


# Ultima attivita' di ogni sessione, tenuta dal server e non nel cookie: cosi' il cookie di sessione si scrive solo
# all'accesso e all'uscita (e al cambio di lingua), mai durante la navigazione. Bug reale visto con l'inoltro porte
# di Codespaces e piu' schede: ogni riscrittura del cookie a ogni pagina poteva far tornare nel browser una versione
# vecchia (di un altro utente, o gia' scaduta), e l'utente si ritrovava fuori o con l'utente sbagliato.
# Dopo un riavvio del server le sessioni ripartono come attive (si perde solo il conteggio dell'inattivita').
_LAST_SEEN: dict[str, int] = {}
_LOCK = threading.Lock()
# sessioni chiuse con «Esci», sostituite da un nuovo accesso o scadute: salvate su file, cosi' un cookie vecchio non
# torna valido nemmeno dopo un riavvio del server
_REVOKED_FILE = DATA_DIR / "secret" / "revoked_sessions.txt"
_REVOKED_KEEP = 20000


def _load_revoked() -> set[str]:
    try:
        return {x.strip() for x in _REVOKED_FILE.read_text(encoding="utf-8").splitlines() if x.strip()}
    except OSError:
        return set()


_REVOKED: set[str] = _load_revoked()


def _revoke(sid: str) -> None:
    """Da chiamare con _LOCK preso."""
    if sid in _REVOKED:
        return
    _REVOKED.add(sid)
    try:
        _REVOKED_FILE.parent.mkdir(parents=True, exist_ok=True)
        lines = _REVOKED_FILE.read_text(encoding="utf-8").splitlines() if _REVOKED_FILE.exists() else []
        lines = (lines + [sid])[-_REVOKED_KEEP:]
        _REVOKED_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass   # resta revocata in memoria


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def start_session(request, user: User) -> None:
    lang = request.session.get("lang")  # la lingua scelta prima di accedere resta
    end_session(request)   # la sessione precedente di questo browser non vale piu'
    request.session.clear()
    sid = secrets.token_urlsafe(16)
    request.session["user_id"] = user.id
    request.session["sid"] = sid
    with _LOCK:
        _LAST_SEEN[sid] = int(time.time())
    if user.language or lang:
        request.session["lang"] = user.language or lang


def end_session(request) -> None:
    """La sessione del browser non vale piu' (anche se il browser rimandasse il suo cookie)."""
    sid = request.session.get("sid")
    if sid:
        with _LOCK:
            _LAST_SEEN.pop(sid, None)
            _revoke(sid)


def session_expired(request) -> bool:
    """Il browser manda una sessione con un utente che non vale piu' (inattivita' o uscita): per l'avviso al login."""
    return bool(request.session.get("user_id")) and current_user(request) is None


def current_user(request) -> User | None:
    """Utente loggato, o None (nessun accesso, sessione chiusa, o inattiva da piu' di SESSION_IDLE_MINUTES).
    Non scrive mai il cookie: l'ultima attivita' e' tenuta dal server (_LAST_SEEN)."""
    user_id = request.session.get("user_id")
    sid = request.session.get("sid")
    if not user_id or not sid:
        return None
    now = int(time.time())
    with _LOCK:
        if sid in _REVOKED:
            return None
        last_seen = _LAST_SEEN.get(sid, now)
        if now - last_seen > SESSION_IDLE_MINUTES * 60:
            _LAST_SEEN.pop(sid, None)
            _revoke(sid)
            return None
        _LAST_SEEN[sid] = now
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


def has_processes(user) -> bool:
    """True se l'utente ha almeno un ruolo su un processo (Repository PM: Data Engineer o Data Analyst)."""
    if user is None or getattr(user, "is_admin", False):
        return False
    db = SessionLocal()
    try:
        return db.query(ProcessAssignment).filter_by(user_id=user.id).first() is not None
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
