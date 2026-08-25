"""Validation / Data Quality Engine (Fase F del Modulo 1).

Gira sull'OCEL log gia' prodotto (piu' il log degli scarti della
trasformazione) e restituisce una lista di esiti, ciascuno con severita',
esito pass/fail e un contatore di elementi coinvolti: e' quello che nella
UI di review appare come warning/errori da guardare prima di consolidare
la Ingestion Config.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from app.services.transformation import SkipRecord

# Object type scelto come "case notion" per questi check (P2P: l'ordine d'acquisto).
# Semplificazione nota: in questo prototipo è fisso; una piattaforma multi-processo
# lo renderebbe configurabile per Ingestion Config.
CASE_OBJECT_TYPE = "PurchaseOrder"

# Qualifier assegnato dal Transformation Engine al collegamento evento->oggetto
# "nativo" (evento generato dalla stessa riga/tabella che ha creato l'oggetto).
# Usarlo invece del nome dell'event type rende i check indipendenti da come
# l'AI Mapping Service (mock o LLM reale) ha chiamato l'evento di creazione:
# un mapper diverso puo' chiamarlo "Create Purchase Order", "PO Created", ecc.
HOME_QUALIFIER = "involves"


def _check_missing_timestamps(skip_log: list[SkipRecord]) -> dict:
    by_type = defaultdict(int)
    for s in skip_log:
        by_type[s.event_type] += 1
    total = sum(by_type.values())
    details = "; ".join(f"{k}: {v} righe scartate" for k, v in by_type.items()) or "nessuna riga scartata"
    return {
        "check_name": "Timestamp mancante o non valido",
        "severity": "warning",
        "passed": total == 0,
        "details": details,
        "affected_count": total,
    }


def _check_events_before_case_start(ocel: dict) -> dict:
    prefix = f"{CASE_OBJECT_TYPE}:"
    case_start: dict[str, datetime] = {}
    for e in ocel["events"]:
        t = datetime.strptime(e["time"], "%Y-%m-%dT%H:%M:%SZ")
        for rel in e["relationships"]:
            if rel["qualifier"] == HOME_QUALIFIER and rel["objectId"].startswith(prefix):
                case_start[rel["objectId"]] = min(t, case_start.get(rel["objectId"], t))

    violations = []
    for e in ocel["events"]:
        t = datetime.strptime(e["time"], "%Y-%m-%dT%H:%M:%SZ")
        for rel in e["relationships"]:
            obj_id = rel["objectId"]
            if obj_id in case_start and t < case_start[obj_id]:
                violations.append(f"{e['type']} ({e['time']}) su {obj_id}, creato il {case_start[obj_id].strftime('%Y-%m-%d')}")

    details = "; ".join(violations[:10]) if violations else "nessuna anomalia rilevata"
    if len(violations) > 10:
        details += f" (+{len(violations) - 10} altre)"
    return {
        "check_name": "Evento antecedente alla creazione del case",
        "severity": "error",
        "passed": len(violations) == 0,
        "details": details,
        "affected_count": len(violations),
    }


def _check_orphan_objects(ocel: dict) -> dict:
    referenced = {rel["objectId"] for e in ocel["events"] for rel in e["relationships"]}
    all_ids = {o["id"] for o in ocel["objects"]}
    orphans = sorted(all_ids - referenced)
    return {
        "check_name": "Oggetti senza alcun evento collegato",
        "severity": "warning",
        "passed": len(orphans) == 0,
        "details": ", ".join(orphans[:10]) + (f" (+{len(orphans) - 10} altri)" if len(orphans) > 10 else "") if orphans else "nessun oggetto orfano",
        "affected_count": len(orphans),
    }


def _check_case_without_creation_event(ocel: dict) -> dict:
    po_ids = {o["id"] for o in ocel["objects"] if o["type"] == CASE_OBJECT_TYPE}
    created_ids = {
        rel["objectId"]
        for e in ocel["events"]
        for rel in e["relationships"] if rel["qualifier"] == HOME_QUALIFIER
    }
    missing = sorted(po_ids - created_ids)
    return {
        "check_name": f"{CASE_OBJECT_TYPE} senza evento di creazione",
        "severity": "error",
        "passed": len(missing) == 0,
        "details": ", ".join(missing) if missing else "ogni PurchaseOrder ha il proprio evento di creazione",
        "affected_count": len(missing),
    }


def _check_event_distribution(ocel: dict) -> dict:
    by_type = defaultdict(int)
    for e in ocel["events"]:
        by_type[e["type"]] += 1
    details = "; ".join(f"{k}: {v}" for k, v in sorted(by_type.items()))
    return {
        "check_name": "Distribuzione eventi per tipo (informativo)",
        "severity": "info",
        "passed": True,
        "details": details,
        "affected_count": len(ocel["events"]),
    }


def run_data_quality_checks(ocel: dict, skip_log: list[SkipRecord]) -> list[dict]:
    return [
        _check_missing_timestamps(skip_log),
        _check_events_before_case_start(ocel),
        _check_orphan_objects(ocel),
        _check_case_without_creation_event(ocel),
        _check_event_distribution(ocel),
    ]
