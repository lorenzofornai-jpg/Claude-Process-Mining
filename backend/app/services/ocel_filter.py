"""Filtri per attributo sul dataset OCEL per l'analisi (Process Explorer, Process Overview, assistente).

Un filtro sceglie gli oggetti «di partenza» con un valore di attributo:
- attributo di oggetto: gli oggetti di quel tipo con il valore (es. ordini con cliente = 1004502);
- attributo di evento: gli oggetti collegati agli eventi con il valore (es. eventi con motivo = PRICE_DISPUTE).
Il dataset filtrato tiene gli oggetti di partenza, gli oggetti collegati a loro da un evento (esclusi i tipi
trasversali, come il cliente, che collegherebbero tutto) e gli eventi di questi oggetti: la fattura di un ordine
del cliente resta con tutta la sua storia, fino al pagamento. Piu' filtri valgono insieme (uno dopo l'altro).

Funziona su qualunque OCEL 2.0 e qualunque processo: gli attributi sono quelli che il mapping ha prodotto.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

MAX_VALUES = 2000
_RAW: dict[tuple[str, float], dict] = {}


def load_raw(path: str | Path) -> dict:
    path = Path(path)
    key = (str(path.resolve()), path.stat().st_mtime)
    if key not in _RAW:
        if len(_RAW) >= 2:
            _RAW.pop(next(iter(_RAW)))
        with open(path, encoding="utf-8") as f:
            _RAW[key] = json.load(f)
    return _RAW[key]


def _object_attrs(o: dict) -> dict[str, str]:
    """Ultimo valore di ogni attributo dell'oggetto."""
    out = {}
    for a in o.get("attributes") or []:
        if a.get("name") is not None and a.get("value") not in (None, ""):
            out[a["name"]] = str(a["value"])
    return out


def _event_attrs(e: dict) -> dict[str, str]:
    return {a["name"]: str(a["value"]) for a in e.get("attributes") or []
            if a.get("name") is not None and a.get("value") not in (None, "")}


def catalog(ocel: dict) -> list[dict]:
    """Attributi su cui si puo' filtrare: per tipo di oggetto e per gli eventi, con i valori (i piu' frequenti
    prima) e quanti oggetti o eventi li hanno."""
    by_type: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for o in ocel.get("objects", []):
        for k, v in _object_attrs(o).items():
            by_type[(o["type"], k)][v] += 1
    out = [{"kind": "object", "type": t, "attribute": a, "distinct": len(c),
            "values": [{"value": v, "count": n} for v, n in c.most_common(MAX_VALUES)]}
           for (t, a), c in by_type.items() if len(c) >= 2]
    ev: dict[str, Counter] = defaultdict(Counter)
    for e in ocel.get("events", []):
        if not e.get("relationships"):
            continue   # un evento senza oggetti non porta a nessun caso: filtrare su di lui non servirebbe
        for k, v in _event_attrs(e).items():
            ev[k][v] += 1
    out += [{"kind": "event", "type": None, "attribute": a, "distinct": len(c),
             "values": [{"value": v, "count": n} for v, n in c.most_common(MAX_VALUES)]}
            for a, c in ev.items() if len(c) >= 2]
    return sorted(out, key=lambda x: (x["kind"] != "object", x["type"] or "", x["attribute"]))


def clean(filters) -> list[dict]:
    """Filtri ricevuti dal browser, normalizzati (ordine stabile: servono anche come chiave di cache)."""
    out = []
    for f in filters or []:
        if not isinstance(f, dict):
            continue
        kind = "event" if f.get("kind") == "event" else "object"
        values = sorted({str(v) for v in f.get("values") or [] if str(v) != ""})[:200]
        attribute = str(f.get("attribute") or "")
        if attribute and values and (kind == "event" or f.get("type")):
            out.append({"kind": kind, "type": str(f.get("type") or "") if kind == "object" else None,
                        "attribute": attribute, "values": values})
    return out


def apply(ocel: dict, filters: list[dict], hubs: set[str] | None = None) -> dict:
    """Il dataset ridotto ai casi che soddisfano tutti i filtri (vedi la descrizione del modulo)."""
    hubs = hubs or set()
    for f in clean(filters):
        type_of = {o["id"]: o["type"] for o in ocel.get("objects", [])}
        values = set(f["values"])
        if f["kind"] == "object":
            seeds = {o["id"] for o in ocel.get("objects", [])
                     if o["type"] == f["type"] and _object_attrs(o).get(f["attribute"]) in values}
        else:
            seeds = {r["objectId"] for e in ocel.get("events", []) if _event_attrs(e).get(f["attribute"]) in values
                     for r in e.get("relationships", []) if type_of.get(r.get("objectId")) not in hubs}
        # oggetti collegati ai casi di partenza da un evento (non i trasversali)
        core = set(seeds)
        for e in ocel.get("events", []):
            ids = [r["objectId"] for r in e.get("relationships", [])]
            if seeds.intersection(ids):
                core.update(i for i in ids if i in type_of and type_of[i] not in hubs)
        events, kept = [], set()
        for e in ocel.get("events", []):
            rels = [r for r in e.get("relationships", []) if r.get("objectId") in type_of]
            if not any(r["objectId"] in core for r in rels):
                continue
            rels = [r for r in rels if r["objectId"] in core or type_of[r["objectId"]] in hubs]
            kept.update(r["objectId"] for r in rels)
            events.append({**e, "relationships": rels})
        kept |= seeds
        ocel = {**ocel, "objects": [o for o in ocel.get("objects", []) if o["id"] in kept], "events": events}
    return ocel


def tables(ocel: dict) -> dict[str, list[dict]]:
    """Il dataset come tabelle per le interrogazioni dell'assistente (data_query): una per tipo di oggetto (id,
    attributi, numero di eventi, prima e ultima data, durata in giorni, attivita' in ordine) e una «events»."""
    type_of = {o["id"]: o["type"] for o in ocel.get("objects", [])}
    seqs: dict[str, list[tuple[str, str]]] = defaultdict(list)
    ev_rows = []
    for e in ocel.get("events", []):
        ids = [r["objectId"] for r in e.get("relationships", []) if r.get("objectId") in type_of]
        for i in ids:
            seqs[i].append((e.get("time", ""), e.get("type", "")))
        ev_rows.append({"id": e.get("id", ""), "activity": e.get("type", ""), "time": e.get("time", ""),
                        "objects": " ".join(ids[:20]), **_event_attrs(e)})
    from datetime import datetime

    def days(a: str, b: str):
        try:
            d = datetime.fromisoformat(b.replace("Z", "+00:00")) - datetime.fromisoformat(a.replace("Z", "+00:00"))
            return f"{d.total_seconds() / 86400:.1f}"
        except ValueError:
            return ""
    out: dict[str, list[dict]] = defaultdict(list)
    for o in ocel.get("objects", []):
        s = sorted(seqs.get(o["id"], []))
        out[o["type"]].append({"id": o["id"], **_object_attrs(o), "events": str(len(s)),
                               "first": s[0][0] if s else "", "last": s[-1][0] if s else "",
                               "duration_days": days(s[0][0], s[-1][0]) if s else "",
                               "activities": " > ".join(a for _, a in s)[:400]})
    out["events"] = ev_rows
    return dict(out)
