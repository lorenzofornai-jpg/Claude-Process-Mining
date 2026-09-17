"""Stato di sessione in-memory per il ciclo di ingestion interattivo.

Chiave = (user_id, workspace_id), non piu' un id di sessione HTTP casuale:
cosi' un Data Engineer puo' riprendere il lavoro su un processo assegnato in
qualsiasi momento (dashboard -> stesso workspace_id -> stesso stato), invece
di dover ripercorrere il wizard dall'inizio in un'unica sessione continua.

L'user_id nella chiave e' voluto, non solo il workspace_id: un admin ha
accesso illimitato a tutti i workspace, quindi puo' aprire il wizard di
ingestion sullo stesso processo su cui sta lavorando il Data Engineer
assegnato. Con la sola chiave workspace_id i due condividerebbero (e si
sovrascriverebbero a vicenda) lo stesso stato in-memory - file caricati,
mapping in revisione, step del wizard - un vero problema di isolamento tra
utenti diversi, non solo un display sbagliato. Con la chiave composita,
ognuno ha il proprio stato indipendente sullo stesso workspace.

Semplificazione da prototipo: stato tenuto in un dict di modulo invece che
in Redis/DB, quindi non sopravvive a un riavvio del server (a differenza di
IngestionConfig/FieldMapping/ExtractionRun, quelli si', vedi models.py).
"""
from __future__ import annotations

from typing import Any

_SESSIONS: dict[tuple[str, str], dict[str, Any]] = {}


def ensure(user_id: str, workspace_id: str) -> dict[str, Any]:
    """Crea lo stato per questo utente su questo workspace se non esiste
    ancora, e lo ritorna."""
    return _SESSIONS.setdefault((user_id, workspace_id), {"workspace_id": workspace_id})


def get(user_id: str, workspace_id: str) -> dict[str, Any]:
    key = (user_id, workspace_id)
    if key not in _SESSIONS:
        raise KeyError(f"Stato di ingestion non trovato per utente {user_id} / workspace {workspace_id}")
    return _SESSIONS[key]


def exists(user_id: str, workspace_id: str) -> bool:
    return (user_id, workspace_id) in _SESSIONS
