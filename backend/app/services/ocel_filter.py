"""Filtri per attributo sul dataset OCEL per l'analisi (Process Explorer, Process Overview, assistente).

Un filtro sceglie gli oggetti «di partenza» con un valore di attributo:
- attributo di oggetto: gli oggetti di quel tipo con il valore (es. ordini con cliente = 1004502);
- attributo di evento: gli oggetti collegati agli eventi con il valore (es. eventi con motivo = PRICE_DISPUTE).
Il dataset filtrato tiene gli oggetti di partenza, gli oggetti collegati a loro da un evento (esclusi i tipi
trasversali, come il cliente, che collegherebbero tutto) e gli eventi di questi oggetti: la fattura di un ordine
del cliente resta con tutta la sua storia, fino al pagamento. Piu' filtri valgono insieme (uno dopo l'altro).

Altri filtri sui casi (selezioni fatte sui grafici delle analisi, condivise tra le pagine dello stesso dataset):
- mese di inizio: gli oggetti di un tipo con il primo evento in uno dei mesi (es. ordini iniziati a gen o feb);
- tempo di attraversamento: gli oggetti di un tipo con il tempo dal primo all'ultimo evento (solo l'oggetto, o con
  gli oggetti collegati non trasversali) in uno degli intervalli di giorni scelti.
Per i filtri su un tipo di oggetto, gli altri oggetti dello stesso tipo collegati per caso (es. due ordini nella
stessa fattura) non entrano: il filtro «gennaio» non porta con se' un ordine di marzo.

Funziona su qualunque OCEL 2.0 e qualunque processo: gli attributi sono quelli che il mapping ha prodotto.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
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


CASE_KINDS = ("start_month", "duration")
# selezioni sui grafici della Control Tower: si risolvono in oggetti («ids») con il modello del dataset
# (explorer.load_model), con la stessa logica dei grafici (control_tower.dimension_values / holds)
MODEL_KINDS = ("dimension", "condition")
DIMENSION_KEYS = ("source", "attribute", "object_type", "attributes", "activities", "related")
MAX_IDS = 200000


def _small_json(v, limit: int = 20000) -> bool:
    try:
        return len(json.dumps(v, ensure_ascii=False)) <= limit
    except (TypeError, ValueError):
        return False


def clean(filters) -> list[dict]:
    """Filtri ricevuti dal browser, normalizzati (ordine stabile: servono anche come chiave di cache)."""
    out = []
    for f in filters or []:
        if not isinstance(f, dict):
            continue
        kind = f.get("kind")
        if kind == "start_month":
            months = sorted({str(v) for v in f.get("values") or [] if len(str(v)) == 7 and str(v)[4] == "-"})[:240]
            if f.get("type") and months:
                out.append({"kind": kind, "type": str(f["type"]), "values": months})
            continue
        if kind == "duration":
            ranges = []
            for r in f.get("ranges") or []:
                try:
                    a = float(r[0])
                    b = None if r[1] is None else float(r[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if math.isfinite(a) and (b is None or (math.isfinite(b) and b >= a)):
                    ranges.append([round(a, 6), None if b is None else round(b, 6)])
            if f.get("type") and ranges:
                out.append({"kind": kind, "type": str(f["type"]),
                            "scope": "related" if f.get("scope") == "related" else "object",
                            "ranges": sorted(ranges, key=lambda r: r[0])[:50]})
            continue
        if kind == "dimension":
            d = f.get("dimension")
            if f.get("type") and isinstance(d, dict) and d.get("source") and _small_json(d):
                values = sorted({str(v) for v in f.get("values") or []})[:500]
                if values:
                    out.append({"kind": kind, "type": str(f["type"]),
                                "dimension": {k: d[k] for k in DIMENSION_KEYS if k in d},
                                "values": values, "key": str(f.get("key") or "")[:300],
                                "title": str(f.get("title") or "")[:200]})
            continue
        if kind == "condition":
            c = f.get("condition")
            if f.get("type") and isinstance(c, dict) and c.get("op") and _small_json(c):
                label = f.get("label")
                out.append({"kind": kind, "type": str(f["type"]), "condition": c, "key": str(f.get("key") or "")[:300],
                            "label": label if isinstance(label, (str, dict)) else "", "picked": [
                                int(x) for x in (f.get("picked") or []) if isinstance(x, (int, float))][:100]})
            continue
        if kind == "ids":   # solo interno (risolto da explorer.load_model)
            ids = [str(x) for x in f.get("ids") or []][:MAX_IDS]
            out.append({"kind": kind, "type": str(f.get("type") or ""), "ids": sorted(set(ids))})
            continue
        kind = "event" if kind == "event" else "object"
        values = sorted({str(v) for v in f.get("values") or [] if str(v) != ""})[:200]
        attribute = str(f.get("attribute") or "")
        if attribute and values and (kind == "event" or f.get("type")):
            out.append({"kind": kind, "type": str(f.get("type") or "") if kind == "object" else None,
                        "attribute": attribute, "values": values})
    return out


def _ts(value) -> float | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _case_seeds(ocel: dict, f: dict, type_of: dict[str, str], hubs: set[str]) -> set[str]:
    """Oggetti di partenza per i filtri sui casi (mese di inizio, tempo di attraversamento)."""
    times: dict[str, list[float]] = defaultdict(list)
    linked: dict[str, set[str]] = defaultdict(set)
    want = f["kind"] == "duration" and f["scope"] == "related"
    for e in ocel.get("events", []):
        t = _ts(e.get("time"))
        ids = [r["objectId"] for r in e.get("relationships", []) if r.get("objectId") in type_of]
        for i in ids:
            if t is not None:
                times[i].append(t)
            if want and type_of[i] == f["type"]:
                linked[i].update(x for x in ids if x != i and type_of[x] not in hubs)
    seeds = set()
    for oid, ty in type_of.items():
        if ty != f["type"] or not times.get(oid):
            continue
        own = times[oid]
        if f["kind"] == "start_month":
            if datetime.fromtimestamp(min(own), tz=timezone.utc).strftime("%Y-%m") in f["values"]:
                seeds.add(oid)
            continue
        ts = list(own)
        if want:
            for x in linked.get(oid, ()):
                ts += times.get(x, [])
        days = (max(ts) - min(ts)) / 86400
        if any(r[0] - 1e-9 <= days and (r[1] is None or days <= r[1] + 1e-9) for r in f["ranges"]):
            seeds.add(oid)
    return seeds


def apply(ocel: dict, filters: list[dict], hubs: set[str] | None = None) -> dict:
    """Il dataset ridotto ai casi che soddisfano tutti i filtri (vedi la descrizione del modulo)."""
    hubs = hubs or set()
    for f in clean(filters):
        type_of = {o["id"]: o["type"] for o in ocel.get("objects", [])}
        if f["kind"] in CASE_KINDS:
            seeds = _case_seeds(ocel, f, type_of, hubs)
        elif f["kind"] == "ids":
            wanted = set(f["ids"])
            seeds = {i for i, t in type_of.items() if t == f["type"] and i in wanted}
        elif f["kind"] in MODEL_KINDS:
            continue   # va risolto prima con il modello (explorer.load_model)
        elif f["kind"] == "object":
            values = set(f["values"])
            seeds = {o["id"] for o in ocel.get("objects", [])
                     if o["type"] == f["type"] and _object_attrs(o).get(f["attribute"]) in values}
        else:
            values = set(f["values"])
            seeds = {r["objectId"] for e in ocel.get("events", []) if _event_attrs(e).get(f["attribute"]) in values
                     for r in e.get("relationships", []) if type_of.get(r.get("objectId")) not in hubs}
        # oggetti collegati ai casi di partenza da un evento (non i trasversali; per un filtro su un tipo, non gli
        # altri oggetti dello stesso tipo)
        same = f.get("type") if f["kind"] != "event" else None
        core = set(seeds)
        for e in ocel.get("events", []):
            ids = [r["objectId"] for r in e.get("relationships", [])]
            if seeds.intersection(ids):
                core.update(i for i in ids if i in type_of and type_of[i] not in hubs and type_of[i] != same)
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
