"""Stato di sessione in-memory per il ciclo di ingestion interattivo.

Chiave = workspace_id (id del ProcessWorkspace nel DB), non piu' un id di
sessione HTTP casuale: cosi' un Data Engineer puo' riprendere il lavoro su
un processo assegnato in qualsiasi momento (dashboard -> stesso workspace_id
-> stesso stato), invece di dover ripercorrere il wizard dall'inizio in
un'unica sessione continua.

Semplificazione da prototipo: stato tenuto in un dict di modulo invece che
in Redis/DB, quindi non sopravvive a un riavvio del server (a differenza di
IngestionConfig/FieldMapping/ExtractionRun, quelli si', vedi models.py).
"""
from __future__ import annotations

from typing import Any

_SESSIONS: dict[str, dict[str, Any]] = {}


def ensure(workspace_id: str) -> dict[str, Any]:
    """Crea lo stato per questo workspace se non esiste ancora, e lo ritorna."""
    return _SESSIONS.setdefault(workspace_id, {"workspace_id": workspace_id})


def get(workspace_id: str) -> dict[str, Any]:
    if workspace_id not in _SESSIONS:
        raise KeyError(f"Stato di ingestion non trovato per workspace: {workspace_id}")
    return _SESSIONS[workspace_id]


def exists(workspace_id: str) -> bool:
    return workspace_id in _SESSIONS
