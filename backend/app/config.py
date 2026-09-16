import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")  # solo per sviluppo locale; mai committato (vedi .gitignore)

# Firma i cookie di sessione. Se non impostata in .env, generata ad ogni avvio:
# le sessioni non sopravvivono a un riavvio del server (accettabile per il
# prototipo; in produzione va fissata via env/secret manager).
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY") or secrets.token_urlsafe(32)

# Credenziali dell'unico utente che esiste al primissimo avvio dell'app per
# un nuovo cliente (nessun altro utente ancora creato): di default
# superuser/superuser, da cambiare come primissima cosa dopo il primo
# accesso (nessun cambio forzato lato app per ora: e' una scelta operativa,
# non un limite tecnico). Personalizzabili via ADMIN_EMAIL/ADMIN_PASSWORD in
# backend/.env per chi preferisce non usare il default.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "superuser")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "superuser")
# Solo per distinguere nei log "sto usando superuser/superuser di default"
# da "queste credenziali arrivano da un .env vero" - non cambia il
# comportamento, solo il messaggio stampato all'avvio.
ADMIN_CREDENTIALS_FROM_ENV = "ADMIN_EMAIL" in os.environ or "ADMIN_PASSWORD" in os.environ

DATA_DIR = BASE_DIR / "data"
SYNTHETIC_P2P_DIR = DATA_DIR / "synthetic_p2p"
DB_PATH = BASE_DIR / "ingestion.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

# Sotto questa soglia di confidence una proposta di mapping non viene
# auto-accettata in bulk: richiede revisione/decisione esplicita dell'utente.
AUTO_ACCEPT_CONFIDENCE_THRESHOLD = 0.85

# "claude" (default: vera chiamata LLM, richiede ANTHROPIC_API_KEY in ambiente/.env)
# o "heuristic" (mock a catalogo fisso di 4 tabelle, nessuna chiamata esterna,
# utile solo per sviluppo offline). Se AI_MAPPER=claude ma manca la chiave, il
# router fa fallback automatico su "heuristic" con un avviso nei log e in UI:
# non e' pensato per l'uso normale, solo per non bloccare chi non ha ancora
# configurato una chiave.
AI_MAPPER = os.environ.get("AI_MAPPER", "claude")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")

# Cache-busting per gli asset statici (style.css): senza una query string che
# cambia, il browser puo' continuare a servire una versione in cache anche
# dopo un git pull con CSS aggiornato - visto succedere davvero in test
# (l'utente vedeva l'interfaccia "vecchia" nonostante il codice fosse
# aggiornato). L'mtime del file cambia ad ogni modifica (e tipicamente anche
# ad ogni checkout git), quindi basta come versione senza dover incrementare
# un numero a mano.
_STYLE_CSS_PATH = BASE_DIR / "app" / "static" / "style.css"
STATIC_VERSION = str(int(_STYLE_CSS_PATH.stat().st_mtime)) if _STYLE_CSS_PATH.exists() else "0"
