"""Accesso, uscita e nuovo accesso con un altro utente nello stesso browser (routers/login.py, app/auth.py).
Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import auth, models  # noqa: E402,F401
from app.db import Base  # noqa: E402
from app.models import User  # noqa: E402


def _next(r) -> str:
    """Accesso e uscita rispondono con una pagina che porta subito alla successiva (non con un redirect)."""
    assert r.status_code == 200
    return re.search(r'url=([^"]+)"', r.text).group(1).replace("&amp;", "&")


def test_login_logout_and_login_as_another_user(tmp_path, monkeypatch):
    import app.main as main
    import app.routers.bpa_repository as br
    import app.routers.login as login
    e = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(e)
    S = sessionmaker(bind=e)
    db = S()
    db.add(User(id="a", name="Alcaraz", email="a@x.it", password_hash=auth.hash_password("pw-alcaraz"), bpa_role="architect"))
    db.add(User(id="s", name="Sinner", email="s@x.it", password_hash=auth.hash_password("pw-sinner"), bpa_role="viewer"))
    db.commit()
    db.close()
    for mod in (auth, login, br):
        monkeypatch.setattr(mod, "SessionLocal", S)
    monkeypatch.setattr(auth, "_REVOKED_FILE", tmp_path / "revoked.txt")
    monkeypatch.setattr(auth, "_REVOKED", set())
    c = TestClient(main.app)
    assert _next(c.post("/login", data={"email": "a@x.it", "password": "pw-alcaraz"})).startswith("/bpa?_s=")
    old = dict(c.cookies)
    assert c.get("/bpa/api/tree").status_code == 200
    assert _next(c.post("/logout")).startswith("/login?logged_out=1")
    assert c.get("/bpa/api/tree").status_code == 401
    assert _next(c.post("/login", data={"email": "s@x.it", "password": "pw-sinner"})).startswith("/bpa?_s=")
    assert c.get("/bpa/api/tree").status_code == 200 and c.get("/bpa/m/meta").status_code == 200
    # il cookie di Alcaraz, se il browser lo rimandasse, non vale piu'
    other = TestClient(main.app)
    other.cookies.update(old)
    assert other.get("/bpa/api/tree").status_code == 401


def test_cookies_split_in_several_headers_are_read(tmp_path, monkeypatch):
    """Con HTTP/2 (inoltro porte di Codespaces) i cookie possono arrivare in piu' intestazioni: la sessione vale lo stesso."""
    import asyncio
    import app.main as main
    seen = {}

    async def inner(scope, receive, send):
        seen["cookie"] = [v for k, v in scope["headers"] if k == b"cookie"]

    mw = main._MergeCookieHeaders(inner)
    scope = {"type": "http", "headers": [(b"host", b"x"), (b"cookie", b"gh_tunnel=1"), (b"cookie", b"session=abc"),
                                         (b"cookie", b"other=2")]}
    asyncio.run(mw(scope, None, None))
    assert seen["cookie"] == [b"gh_tunnel=1; session=abc; other=2"]
    # e l'app legge davvero la sessione: accesso, poi richiesta con il cookie di sessione non per primo
    import app.routers.bpa_repository as br
    import app.routers.login as login
    e = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(e)
    S = sessionmaker(bind=e)
    db = S()
    db.add(User(id="a", name="Alcaraz", email="a@x.it", password_hash=auth.hash_password("pw-alcaraz"), bpa_role="architect"))
    db.commit()
    db.close()
    for mod in (auth, login, br):
        monkeypatch.setattr(mod, "SessionLocal", S)
    c = TestClient(main.app)
    c.post("/login", data={"email": "a@x.it", "password": "pw-alcaraz"})
    value = c.cookies.get("session")
    raw = TestClient(main.app)
    r = raw.get("/bpa/api/tree", headers=[("cookie", "gh_tunnel=1"), ("cookie", f"session={value}")])
    assert r.status_code == 200
