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

# Credenziali dell'admin iniziale, creato al primo avvio se non esiste gia'
# un utente admin. Se ADMIN_PASSWORD non e' impostata, viene generata a
# caso e stampata una sola volta nei log di avvio.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

DATA_DIR = BASE_DIR / "data"
SYNTHETIC_P2P_DIR = DATA_DIR / "synthetic_p2p"
DB_PATH = BASE_DIR / "ingestion.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

# Sotto questa soglia di confidence una proposta di mapping non viene
# auto-accettata in bulk: richiede revisione/decisione esplicita dell'utente.
AUTO_ACCEPT_CONFIDENCE_THRESHOLD = 0.85

# "heuristic" (default, nessuna chiamata esterna) o "claude" (vera chiamata LLM,
# richiede ANTHROPIC_API_KEY in ambiente/.env).
AI_MAPPER = os.environ.get("AI_MAPPER", "heuristic")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
