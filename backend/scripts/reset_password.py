"""Reimposta la password di un utente dal terminale, per quando nessun amministratore riesce ad accedere (es. password
del superuser dimenticata). Va lanciato da chi ha accesso al server (o al Codespace), dalla cartella backend:

    python scripts/reset_password.py                 # elenca gli amministratori e chiede quale reimpostare
    python scripts/reset_password.py --email superuser

La nuova password si scrive due volte e non compare a schermo. Tocca solo la password di quell'utente.
Uso: python scripts/reset_password.py [--email EMAIL]
"""
from __future__ import annotations

import argparse
import sys
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import hash_password  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import User  # noqa: E402
from app.routers.admin import MIN_PASSWORD  # noqa: E402


def choose_admin(db) -> User | None:
    admins = db.query(User).filter_by(is_admin=True).order_by(User.created_at).all()
    if not admins:
        print("Nessun amministratore nel database: al prossimo avvio l'app crea superuser / superuser.")
        return None
    for i, u in enumerate(admins, 1):
        print(f"{i}. {u.name} — {u.email}")
    raw = input("Numero dell'amministratore da reimpostare: ").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= len(admins):
        print("Scelta non valida: niente è cambiato.")
        return None
    return admins[int(raw) - 1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reimposta la password di un utente.")
    parser.add_argument("--email", help="email (o nome utente, es. superuser) dell'utente; senza: scegli tra gli amministratori")
    args = parser.parse_args(argv)
    init_db()
    db = SessionLocal()
    try:
        if args.email:
            user = db.query(User).filter_by(email=args.email.strip().lower()).first()
            if user is None:
                print(f"Nessun utente con email «{args.email}».")
                return 1
        else:
            user = choose_admin(db)
            if user is None:
                return 1
        pwd = getpass(f"Nuova password per {user.email} (almeno {MIN_PASSWORD} caratteri): ")
        if len(pwd) < MIN_PASSWORD:
            print(f"La password deve avere almeno {MIN_PASSWORD} caratteri: niente è cambiato.")
            return 1
        if pwd != getpass("Ripeti la password: "):
            print("Le password non coincidono: niente è cambiato.")
            return 1
        user.password_hash = hash_password(pwd)
        db.commit()
        print(f"Password reimpostata per {user.email}: vale dal prossimo accesso.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
