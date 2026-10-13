"""Configurazione dell'LLM, in un punto solo: client, modello, effort e prezzi per ogni uso dell'app.

L'amministratore la imposta nella pagina Amministrazione > LLM (routers/admin.py); finche' non la salva valgono i
valori dell'ambiente del server (app/config.py, file .env). La chiave API inserita nella pagina si salva cifrata
con Fernet (AES-128-CBC + HMAC-SHA256); la chiave di cifratura e' APP_SECRET_KEY nell'ambiente oppure un file
generato al primo uso in data/secret/ (fuori da git, leggibile solo dal processo del server).

Usi:
- mapping: proposta di mapping delle tabelle (Modulo 1), con tetto di spesa per elaborazione;
- relevance: controllo che le tabelle caricate c'entrino con il processo (chiamata piccola);
- control_tower: proposta delle definizioni della Control Tower;
- assistant: assistenti, spiegazioni, copertura degli obiettivi, oggetti di business, traduzioni.
"""
from __future__ import annotations

import base64
import os
import threading
from datetime import datetime, timezone

from app.config import (AI_MAPPING_BUDGET_USD, AI_MAPPING_EFFORT, ANTHROPIC_MODEL, CONTROL_TOWER_MODEL, DATA_DIR,
                        EXPLAIN_MODEL, RELEVANCE_CHECK_MODEL)

USES = ("mapping", "relevance", "control_tower", "assistant")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# valori di partenza (dall'ambiente del server) e limiti di output di base per uso
DEFAULT_USES = {
    "mapping": {"model": ANTHROPIC_MODEL, "effort": AI_MAPPING_EFFORT, "max_tokens": None},
    "relevance": {"model": RELEVANCE_CHECK_MODEL, "effort": "low", "max_tokens": None},
    "control_tower": {"model": CONTROL_TOWER_MODEL, "effort": "medium", "max_tokens": None},
    "assistant": {"model": EXPLAIN_MODEL, "effort": "low", "max_tokens": None},
}

# listino Anthropic, USD per milione di token (input, output); l'amministratore puo' cambiarlo o aggiungere modelli
PRICES_USD_PER_MTOK = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
FALLBACK_PRICE = (5.0, 25.0)   # modello sconosciuto: listino alto, cosi' stime e tetti restano prudenti

_lock = threading.Lock()
_cache: dict | None = None


# ---------- cifratura della chiave API ----------

def _fernet():
    from cryptography.fernet import Fernet

    secret = os.environ.get("APP_SECRET_KEY", "").strip()
    if secret:
        # qualunque testo: se non e' gia' una chiave Fernet se ne ricava una (SHA-256)
        try:
            return Fernet(secret.encode())
        except ValueError:
            import hashlib
            return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
    path = DATA_DIR / "secret" / "llm.key"
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    return Fernet(path.read_bytes().strip())


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str | None:
    """None se il testo cifrato non si apre (es. chiave di cifratura cambiata)."""
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def hint(key: str) -> str:
    return "…" + key[-4:] if len(key) >= 8 else "…"


# ---------- lettura ----------

def _load() -> dict:
    """Impostazioni effettive (database, altrimenti ambiente del server), in cache fino al prossimo salvataggio."""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        from app.db import SessionLocal
        from app.models import LLMSettings

        row = None
        try:
            db = SessionLocal()
            try:
                row = db.get(LLMSettings, "default")
            finally:
                db.close()
        except Exception:   # tabella non ancora creata (prima dell'avvio): valori dell'ambiente
            row = None
        uses = {u: dict(DEFAULT_USES[u]) for u in USES}
        out = {"saved": row is not None, "enabled": True, "api_key": None, "api_key_hint": None, "key_error": False,
               "base_url": None, "timeout_s": 600, "max_retries": 2, "uses": uses,
               "prices": {m: list(p) for m, p in PRICES_USD_PER_MTOK.items()},
               "mapping_budget_usd": AI_MAPPING_BUDGET_USD, "updated_by": None, "updated_at": None}
        if row is not None:
            out.update(enabled=bool(row.enabled), base_url=row.base_url or None, timeout_s=row.timeout_s or 600,
                       max_retries=row.max_retries if row.max_retries is not None else 2,
                       mapping_budget_usd=row.mapping_budget_usd if row.mapping_budget_usd is not None else AI_MAPPING_BUDGET_USD,
                       api_key_hint=row.api_key_hint, updated_by=row.updated_by, updated_at=row.updated_at)
            for u, v in (row.uses or {}).items():
                if u in uses and isinstance(v, dict):
                    uses[u].update({k: v[k] for k in ("model", "effort", "max_tokens") if k in v and v[k] not in ("", None)})
            for m, p in (row.prices or {}).items():
                if isinstance(p, (list, tuple)) and len(p) == 2:
                    out["prices"][m] = [float(p[0]), float(p[1])]
            if row.api_key_encrypted:
                key = decrypt(row.api_key_encrypted)
                out["api_key"], out["key_error"] = key, key is None
        _cache = out
        return out


def reload() -> None:
    global _cache
    with _lock:
        _cache = None


def settings() -> dict:
    return _load()


def model(use: str) -> str:
    return _load()["uses"][use]["model"]


def effort(use: str) -> str:
    return _load()["uses"][use]["effort"]


def max_tokens(use: str, default: int) -> int:
    """Limite di output di una chiamata: quello del modulo, ridotto se l'amministratore ha messo un tetto."""
    cap = _load()["uses"][use].get("max_tokens")
    return min(default, int(cap)) if cap else default


def price(model_name: str) -> tuple[float, float]:
    p = _load()["prices"].get(model_name)
    return (float(p[0]), float(p[1])) if p else FALLBACK_PRICE


def mapping_budget_usd() -> float:
    return float(_load()["mapping_budget_usd"])


def key_source() -> str:
    """Da dove arriva la chiave: "saved" (questa pagina), "environment" (server), "none"."""
    s = _load()
    if s["api_key"]:
        return "saved"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "environment"
    return "none"


class LLMDisabled(RuntimeError):
    """L'amministratore ha disattivato l'LLM."""


def client():
    """Client Anthropic con le impostazioni dell'amministratore (chiave, indirizzo, timeout, tentativi)."""
    import anthropic

    s = _load()
    if not s["enabled"]:
        raise LLMDisabled("LLM disattivato dall'amministratore")
    kwargs = {"timeout": float(s["timeout_s"]), "max_retries": int(s["max_retries"])}
    if s["api_key"]:
        kwargs["api_key"] = s["api_key"]
    if s["base_url"]:
        kwargs["base_url"] = s["base_url"]
    return anthropic.Anthropic(**kwargs)


def available() -> bool:
    """L'LLM e' utilizzabile (attivo e con credenziali)?"""
    try:
        client()
        return True
    except Exception:
        return False


# ---------- salvataggio (pagina dell'amministratore) ----------

def save(*, user: str, enabled: bool, api_key: str | None, remove_key: bool, base_url: str | None, timeout_s: int,
         max_retries: int, uses: dict, prices: dict, mapping_budget_usd: float) -> None:
    from app.db import SessionLocal
    from app.models import LLMSettings

    db = SessionLocal()
    try:
        row = db.get(LLMSettings, "default") or LLMSettings(id="default")
        row.enabled, row.base_url = enabled, (base_url or None)
        row.timeout_s, row.max_retries = timeout_s, max_retries
        row.uses, row.prices, row.mapping_budget_usd = uses, prices, mapping_budget_usd
        if remove_key:
            row.api_key_encrypted, row.api_key_hint = None, None
        elif api_key:
            row.api_key_encrypted, row.api_key_hint = encrypt(api_key), hint(api_key)
        row.updated_by, row.updated_at = user, datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(row)
        db.commit()
    finally:
        db.close()
    reload()


def test_connection() -> list[dict]:
    """Verifica chiave, indirizzo e modelli: per ogni modello configurato lo chiede all'API (nessun costo)."""
    out = []
    try:
        c = client()
    except Exception as exc:
        return [{"model": None, "ok": False, "error": str(exc)[:300]}]
    for m in sorted({model(u) for u in USES}):
        try:
            info = c.models.retrieve(m)
            out.append({"model": m, "ok": True, "name": getattr(info, "display_name", None) or m})
        except Exception as exc:
            out.append({"model": m, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"})
    return out
