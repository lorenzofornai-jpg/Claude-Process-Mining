#!/usr/bin/env bash
# Setup ed avvio in un solo comando per macOS/Linux (richiede Python 3.11+).
# Uso: bash backend/run_dev.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creo il virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate
echo "Installo le dipendenze..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

if [ ! -f "data/synthetic_p2p/purchase_orders.csv" ]; then
  echo "Genero il dataset sintetico P2P..."
  python scripts/generate_synthetic_p2p.py
fi

echo ""
echo "Pronto. Apri http://127.0.0.1:8000 nel browser (Ctrl+C per fermare)."
echo ""
uvicorn app.main:app --reload
