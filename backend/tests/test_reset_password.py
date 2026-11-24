"""Reimpostare una password: da Amministrazione (routers/admin.py) e dal terminale (scripts/reset_password.py).
Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import models  # noqa: E402,F401
from app.auth import hash_password, verify_password  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import User  # noqa: E402


@pytest.fixture()
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    db.add(User(id="su", name="Amministratore", email="superuser", password_hash=hash_password("vecchia-pw"), is_admin=True))
    db.add(User(id="a2", name="Anna", email="anna@x.it", password_hash=hash_password("anna-pw-1"), is_admin=True))
    db.commit()
    db.close()
    return S


def _password_ok(S, uid, pwd):
    db = S()
    try:
        return verify_password(pwd, db.get(User, uid).password_hash)
    finally:
        db.close()


def test_admin_resets_another_admins_password(Session, monkeypatch):
    from app.routers import admin
    import app.main as main
    monkeypatch.setattr(admin, "SessionLocal", Session)
    monkeypatch.setattr(admin, "current_user", lambda r: SimpleNamespace(id="a2", name="Anna", is_admin=True))
    c = TestClient(main.app)
    assert c.get("/admin/users/su/password").status_code == 200
    # troppo corta, o le due non coincidono: non cambia nulla
    assert c.post("/admin/users/su/password", data={"password": "corta", "confirm": "corta"}).status_code == 400
    assert c.post("/admin/users/su/password", data={"password": "nuova-pw-1", "confirm": "nuova-pw-2"}).status_code == 400
    assert _password_ok(Session, "su", "vecchia-pw")
    r = c.post("/admin/users/su/password", data={"password": "nuova-pw-1", "confirm": "nuova-pw-1"}, follow_redirects=False)
    assert r.status_code == 303 and "reset=su" in r.headers["location"]
    assert _password_ok(Session, "su", "nuova-pw-1") and _password_ok(Session, "a2", "anna-pw-1")
    # chi non e' amministratore non puo'
    monkeypatch.setattr(admin, "current_user", lambda r: SimpleNamespace(id="x", name="X", is_admin=False))
    assert c.post("/admin/users/su/password", data={"password": "altra-pw-1", "confirm": "altra-pw-1"}).status_code == 403
    assert _password_ok(Session, "su", "nuova-pw-1")


def _script(Session, monkeypatch, answers, passwords):
    spec = importlib.util.spec_from_file_location("reset_password", ROOT / "scripts" / "reset_password.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "SessionLocal", Session)
    monkeypatch.setattr(mod, "init_db", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *_: answers.pop(0))
    monkeypatch.setattr(mod, "getpass", lambda *_: passwords.pop(0))
    return mod


def test_script_resets_by_email_or_by_choice(Session, monkeypatch, capsys):
    mod = _script(Session, monkeypatch, [], ["dal-terminale", "dal-terminale"])
    assert mod.main(["--email", "SUPERUSER"]) == 0 and _password_ok(Session, "su", "dal-terminale")
    # scelta dall'elenco degli amministratori (il secondo e' Anna)
    mod = _script(Session, monkeypatch, ["2"], ["anna-nuova", "anna-nuova"])
    assert mod.main([]) == 0 and _password_ok(Session, "a2", "anna-nuova")
    # password diverse, troppo corta, utente inesistente: niente cambia
    mod = _script(Session, monkeypatch, [], ["primaprima", "secondaaa"])
    assert mod.main(["--email", "superuser"]) == 1
    mod = _script(Session, monkeypatch, [], ["corta"])
    assert mod.main(["--email", "superuser"]) == 1
    mod = _script(Session, monkeypatch, [], [])
    assert mod.main(["--email", "nessuno@x.it"]) == 1
    assert _password_ok(Session, "su", "dal-terminale")


def test_session_cookie_is_not_rewritten_on_every_request(monkeypatch):
    """L'ultima attivita' si aggiorna al massimo una volta al minuto (meno riscritture del cookie di sessione)."""
    from app import auth
    fake = SimpleNamespace(session={"user_id": "u", "last_seen": 1000})
    monkeypatch.setattr(auth, "SessionLocal", lambda: SimpleNamespace(get=lambda *a: "utente", close=lambda: None))
    monkeypatch.setattr(auth.time, "time", lambda: 1030)
    assert auth.current_user(fake) == "utente" and fake.session["last_seen"] == 1000
    monkeypatch.setattr(auth.time, "time", lambda: 1000 + auth.LAST_SEEN_EVERY)
    assert auth.current_user(fake) == "utente" and fake.session["last_seen"] == 1000 + auth.LAST_SEEN_EVERY
    monkeypatch.setattr(auth.time, "time", lambda: 1000 + auth.LAST_SEEN_EVERY + auth.SESSION_IDLE_MINUTES * 60 + 1)
    assert auth.current_user(fake) is None and fake.session.get("expired")
