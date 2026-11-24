#!/usr/bin/env bash
# Avvia l'app in background, se non e' gia' avviata. Il Codespace la lancia da solo a ogni avvio
# (postStartCommand in .devcontainer/devcontainer.json): per una demo basta aprire il collegamento dell'app.
# Log in backend/data/app.log. Per fermarla: bash backend/start_app.sh stop
# Per lavorare con il riavvio automatico a ogni modifica si usa sempre bash backend/run_dev.sh (ferma questa).
cd "$(dirname "$0")"
PID_FILE=data/app.pid
mkdir -p data

running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

if [ "${1:-}" = "stop" ]; then
  if running; then kill "$(cat "$PID_FILE")" && echo "App fermata."; fi
  rm -f "$PID_FILE"
  exit 0
fi

if running; then
  echo "L'app e' gia' avviata (porta 8000)."
  exit 0
fi

if [ ! -x .venv/bin/uvicorn ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
if [ ! -f "data/synthetic_p2p/purchase_orders.csv" ]; then
  .venv/bin/python scripts/generate_synthetic_p2p.py
fi

setsid nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 > data/app.log 2>&1 < /dev/null &
echo $! > "$PID_FILE"
echo "App avviata in background sulla porta 8000 (log: backend/data/app.log)."
