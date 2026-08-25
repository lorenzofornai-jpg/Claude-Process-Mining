from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SYNTHETIC_P2P_DIR = DATA_DIR / "synthetic_p2p"
DB_PATH = BASE_DIR / "ingestion.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

# Sotto questa soglia di confidence una proposta di mapping non viene
# auto-accettata in bulk: richiede revisione/decisione esplicita dell'utente.
AUTO_ACCEPT_CONFIDENCE_THRESHOLD = 0.85
