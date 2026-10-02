"""Anteprima del processo calcolata dal mapping corrente (nessuna chiamata AI).

Prima di confermare il mapping, l'utente vede che processo ne esce davvero:
volumi, passaggi tra attivita' con tempi, collo di bottiglia, varianti, tempi
di attraversamento, un caso reale passo per passo e le dimensioni disponibili
per filtrare le analisi. Un mapping sbagliato qui si vede subito (flussi
assurdi, attivita' mancanti), molto meglio che in una tabella di colonne.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from statistics import median

from app.services.transformation import build_ocel

MAX_DIMENSION_VALUES = 60   # oltre questo numero di valori distinti un attributo non e' una "dimensione"
TOP_VARIANTS = 5
TOP_EDGES = 12


def _ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    days = seconds / 86400
    if days >= 1:
        return f"{days:.1f} gg".replace(".", ",")
    hours = seconds / 3600
    if hours >= 1:
        return f"{hours:.1f} h".replace(".", ",")
    return f"{seconds / 60:.0f} min"


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def choose_case_type(ocel: dict) -> str | None:
    """Oggetto principale proposto: quello collegato al maggior numero di tipi di
    evento (a parita', a piu' eventi)."""
    obj_type = {o["id"]: o["type"] for o in ocel["objects"]}
    event_types_per_obj_type: dict[str, set] = defaultdict(set)
    events_per_obj_type: Counter = Counter()
    for e in ocel["events"]:
        for t in {obj_type.get(r["objectId"]) for r in e["relationships"]} - {None}:
            event_types_per_obj_type[t].add(e["type"])
            events_per_obj_type[t] += 1
    if not event_types_per_obj_type:
        return None
    return max(event_types_per_obj_type, key=lambda t: (len(event_types_per_obj_type[t]), events_per_obj_type[t]))


def build_preview(tables_data: dict, rows: list[dict], case_type: str | None = None) -> dict:
    ocel, skip_log, stats = build_ocel(tables_data, rows)
    objects_by_type = Counter(o["type"] for o in ocel["objects"])
    events_by_type = Counter(e["type"] for e in ocel["events"])
    case_types = sorted(objects_by_type)
    case_type = case_type if case_type in objects_by_type else choose_case_type(ocel)

    obj_type = {o["id"]: o["type"] for o in ocel["objects"]}
    traces: dict[str, list[dict]] = defaultdict(list)
    for e in ocel["events"]:  # gia' ordinati per tempo da build_ocel
        for oid in {r["objectId"] for r in e["relationships"] if obj_type.get(r["objectId"]) == case_type}:
            traces[oid].append(e)

    edges: dict[tuple[str, str], list[float]] = defaultdict(list)
    starts, ends, variants = Counter(), Counter(), Counter()
    throughput, positions = [], defaultdict(list)
    for oid, evs in traces.items():
        seq = [e["type"] for e in evs]
        variants[tuple(seq)] += 1
        starts[seq[0]] += 1
        ends[seq[-1]] += 1
        for i, e in enumerate(evs):
            positions[e["type"]].append(i / max(len(evs) - 1, 1))
        for a, b in zip(evs, evs[1:]):
            edges[(a["type"], b["type"])].append((_ts(b["time"]) - _ts(a["time"])).total_seconds())
        throughput.append((_ts(evs[-1]["time"]) - _ts(evs[0]["time"])).total_seconds())

    case_count = len(traces)
    activity_order = sorted(positions, key=lambda a: median(positions[a]))
    activities = [
        {"name": a, "events": events_by_type[a],
         "cases": sum(1 for evs in traces.values() if any(e["type"] == a for e in evs)),
         "is_start": starts[a] > 0, "is_end": ends[a] > 0}
        for a in activity_order
    ]
    max_events = max((a["events"] for a in activities), default=1)
    for a in activities:
        a["bar_pct"] = round(100 * a["events"] / max_events)
        a["case_pct"] = round(100 * a["cases"] / case_count) if case_count else 0

    edge_list = []
    for (a, b), durs in edges.items():
        edge_list.append({"from": a, "to": b, "count": len(durs), "median_s": median(durs),
                          "median": _fmt_duration(median(durs)), "is_loop": a == b})
    edge_list.sort(key=lambda e: -e["count"])
    edge_list = edge_list[:TOP_EDGES]
    max_edge = max((e["count"] for e in edge_list), default=1)
    for e in edge_list:
        e["bar_pct"] = round(100 * e["count"] / max_edge)
    # collo di bottiglia: il passaggio frequente (almeno il 10% dei casi) con l'attesa mediana piu' lunga
    frequent = [e for e in edge_list if e["count"] >= max(2, 0.1 * case_count) and not e["is_loop"]]
    bottleneck = max(frequent, key=lambda e: e["median_s"]) if frequent else None
    if bottleneck:
        bottleneck["is_bottleneck"] = True

    variant_list = [
        {"steps": list(seq), "count": n, "pct": round(100 * n / case_count) if case_count else 0}
        for seq, n in variants.most_common(TOP_VARIANTS)
    ]
    example = None
    if variant_list:
        top_seq = tuple(variant_list[0]["steps"])
        oid = next(o for o, evs in traces.items() if tuple(e["type"] for e in evs) == top_seq)
        evs = traces[oid]
        example = {"object_id": oid, "steps": [
            {"activity": e["type"], "time": e["time"].replace("T", " ").replace("Z", "")[:16],
             "wait": _fmt_duration((_ts(e["time"]) - _ts(evs[i - 1]["time"])).total_seconds()) if i else ""}
            for i, e in enumerate(evs)
        ]}

    return {
        "case_type": case_type,
        "case_types": case_types,
        "case_count": case_count,
        "objects_by_type": dict(objects_by_type.most_common()),
        "events_total": len(ocel["events"]),
        "events_by_type": dict(events_by_type.most_common()),
        "activities": activities,
        "edges": edge_list,
        "bottleneck": bottleneck,
        "variants": variant_list,
        "variant_count": len(variants),
        "throughput": {"median": _fmt_duration(_pct(throughput, 0.5)), "p90": _fmt_duration(_pct(throughput, 0.9))},
        "example": example,
        "dimensions": _dimensions(ocel),
        "skipped_rows": stats.get("skipped_count", 0),
        "unlinked_events": sum(1 for e in ocel["events"] if not any(obj_type.get(r["objectId"]) == case_type for r in e["relationships"])),
    }


def _is_number(v: str) -> bool:
    try:
        float(str(v).replace(",", "."))
        return True
    except ValueError:
        return False


def _dimensions(ocel: dict) -> list[dict]:
    """Attributi con pochi valori distinti: candidati a filtri/dimensioni di analisi
    (categoria, stabilimento, fornitore, paese, utente...)."""
    values: dict[tuple[str, str, str], Counter] = defaultdict(Counter)
    for o in ocel["objects"]:
        for a in o["attributes"]:
            values[("oggetto", o["type"], a["name"])][a["value"]] += 1
    for e in ocel["events"]:
        for a in e["attributes"]:
            values[("evento", e["type"], a["name"])][a["value"]] += 1
    dims = []
    for (kind, owner, name), counter in values.items():
        n = len(counter)
        total = sum(counter.values())
        numeric = sum(c for v, c in counter.items() if _is_number(v)) / total
        if 2 <= n <= MAX_DIMENSION_VALUES and n < total and numeric < 0.8:  # quantita'/importi non sono dimensioni
            dims.append({"kind": kind, "owner": owner, "name": name, "distinct": n,
                         "examples": [v for v, _ in counter.most_common(4)]})
    return sorted(dims, key=lambda d: (d["kind"] != "oggetto", d["owner"], d["name"]))


def summary_for_ai(preview: dict, rows: list[dict]) -> dict:
    """Sintesi compatta di modello + anteprima per la valutazione delle analisi possibili."""
    model_objects: dict[str, dict] = {}
    model_events: dict[str, dict] = {}
    for r in rows:
        el = r["ocel_element"]
        if el.startswith("object_type") and r.get("object_type"):
            o = model_objects.setdefault(r["object_type"], {"source_table": r["source_table"], "attributes": []})
            if el == "object_type.attribute":
                o["attributes"].append(r.get("attribute_name") or r["source_column"])
        elif el.startswith("event_type") and r.get("event_type"):
            ev = model_events.setdefault(r["event_type"], {"source_table": r["source_table"], "attributes": [], "linked_objects": []})
            if el == "event_type.attribute":
                ev["attributes"].append(r.get("attribute_name") or r["source_column"])
        elif el == "e2o_relationship" and r.get("event_type") and r.get("related_object_type"):
            ev = model_events.setdefault(r["event_type"], {"source_table": r["source_table"], "attributes": [], "linked_objects": []})
            ev["linked_objects"].append(r["related_object_type"])
    return {
        "object_types": model_objects,
        "event_types": model_events,
        "case_object_type": preview["case_type"],
        "cases": preview["case_count"],
        "events_by_type": preview["events_by_type"],
        "activity_order": [a["name"] for a in preview["activities"]],
        "top_transitions": [f"{e['from']} -> {e['to']} ({e['count']}, mediana {e['median']})" for e in preview["edges"]],
        "throughput_time": preview["throughput"],
        "variant_count": preview["variant_count"],
        "dimension_candidates": [
            {"attribute": f"{d['owner']}.{d['name']}", "distinct_values": d["distinct"], "examples": d["examples"]}
            for d in preview["dimensions"]
        ],
    }
