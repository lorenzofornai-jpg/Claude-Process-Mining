"""Stato di sessione in-memory per il ciclo di ingestion interattivo.

Semplificazione da prototipo: niente autenticazione/utenti multipli, un
singolo processo Python, stato tenuto in un dict di modulo invece che in
Redis/DB. Cio' che e' gia' persistito correttamente nel DB relazionale
(vedi models.py) e' solo cio' che sopravvive alla conferma dell'utente:
IngestionConfig, FieldMapping, ExtractionRun, DQ results.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

_SESSIONS: dict[str, dict[str, Any]] = {}


def new_session() -> str:
    session_id = uuid.uuid4().hex[:12]
    _SESSIONS[session_id] = {}
    return session_id


def get(session_id: str) -> dict[str, Any]:
    if session_id not in _SESSIONS:
        raise KeyError(f"Sessione di ingestion non trovata: {session_id}")
    return _SESSIONS[session_id]


def exists(session_id: str) -> bool:
    return session_id in _SESSIONS
