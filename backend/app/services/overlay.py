"""Process Overlay: il BPMN di riferimento della BPA confrontato con i dati del dataset.

Casi: gli oggetti del tipo guida del BPMN con gli eventi degli oggetti collegati (esclusi i trasversali), come le
varianti «con gli oggetti collegati» della Process Overview da cui il BPMN e' nato. Le varianti si possono scegliere
(filtro). Sul BPMN:
- blu: attivita' e collegamenti del modello trovati nei dati (con quanti casi);
- grigio: attivita' e collegamenti del modello non trovati;
- rosso: attivita' dei dati che il modello non ha, e passaggi tra attivita' che nel modello non ci sono.

Attivita' del BPMN -> attivita' del dataset: per i task generati dalla variante l'id (Activity_n = n-esimo passo,
anche se poi rinominato), per gli altri il nome (nel dataset o nelle sue traduzioni).
"""
from __future__ import annotations

import math
import re
import statistics
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

from app.services import bpmn
from app.services.bpa_repo import TAG_TYPE, norm
from app.services.overview import _steps

B = "{%s}" % bpmn.NS["bpmn"]
MAX_EXTRA = 12        # attivita' in rosso disegnate
MAX_EXTRA_EDGES = 40  # collegamenti in rosso disegnati
MAX_VARIANTS = 300


def case_sequences(model, lead: str) -> list[tuple[str, list[str], float | None]]:
    """(oggetto guida, attivita' in ordine di tempo con quelle degli oggetti collegati, durata in secondi)."""
    hubs = {t for t in model.types if model.is_hub(t)}
    out = []
    for oid, seq in model.sequences_by_type.get(lead, []):
        rel = set()
        for _, _, idx in seq:
            rel.update(model.event_objects[idx])
        rel.discard(oid)
        merged = {idx: (ts, a, idx) for ts, a, idx in seq}
        for r in rel:
            if model.type_of[r] in hubs:
                continue
            for ts, a, idx in model.sequences_by_type_index.get(r, ()):
                merged.setdefault(idx, (ts, a, idx))
        evs = sorted(merged.values(), key=lambda x: (math.inf if math.isnan(x[0]) else x[0], x[2]))
        ts = [x[0] for x in evs if not math.isnan(x[0])]
        out.append((oid, [a for _, a, _ in evs], (ts[-1] - ts[0]) if len(ts) > 1 else None))
    return out


def variants(cases) -> list[dict]:
    """Varianti come nella Process Overview (ripetizioni consecutive raggruppate), dalla piu' frequente."""
    groups: dict[tuple, list] = defaultdict(list)
    for oid, acts, dur in cases:
        groups[_steps(acts)].append((oid, acts, dur))
    n = len(cases) or 1
    out = []
    for i, (steps, members) in enumerate(sorted(groups.items(), key=lambda kv: (-len(kv[1]), len(kv[0]), kv[0])), 1):
        durs = [d for _, _, d in members if d is not None]
        out.append({"id": i, "steps": [{"activity": a, "repeated": r} for a, r in steps], "count": len(members),
                    "share": len(members) / n, "median": statistics.median(durs) / 86400 if durs else None,
                    "_members": members})
    return out


def _model_graph(xml: str) -> dict:
    """Nodi (id -> tag, nome) e frecce di sequenza (id, da, a) del BPMN."""
    root = ET.fromstring(xml)
    nodes, flows = {}, []
    for el in root.iter():
        tag = el.tag.split("}", 1)[-1]
        if tag == "sequenceFlow" and el.get("sourceRef") and el.get("targetRef"):
            flows.append((el.get("id"), el.get("sourceRef"), el.get("targetRef")))
        elif tag in TAG_TYPE and el.get("id"):
            nodes[el.get("id")] = {"tag": tag, "type": TAG_TYPE[tag], "name": (el.get("name") or "").strip()}
    return {"nodes": nodes, "flows": flows}


def match_activities(graph: dict, steps: list[dict], labels: dict, data_acts: set[str]) -> dict[str, str]:
    """{id del task nel BPMN: attivita' del dataset}."""
    by_name = {}
    for a in data_acts:
        by_name.setdefault(norm(a), a)
        for v in (labels.get(a) or {}).values():
            if v:
                by_name.setdefault(norm(v), a)
    out = {}
    for nid, n in graph["nodes"].items():
        if n["type"] not in ("activity", "subprocess"):
            continue
        m = re.fullmatch(r"Activity_(\d+)", nid)
        if m and 1 <= int(m.group(1)) <= len(steps):
            out[nid] = steps[int(m.group(1)) - 1]["activity"]   # passo della variante, anche se rinominato
        elif norm(n["name"]) in by_name:
            out[nid] = by_name[norm(n["name"])]
    return out


def compute(model, lead: str, xml: str, steps: list[dict], labels: dict, lang: str,
            variant_ids: list[int] | None = None) -> dict:
    cases = case_sequences(model, lead)
    vs = variants(cases)
    chosen = vs if variant_ids is None else [v for v in vs if v["id"] in set(variant_ids)]
    sel = [m for v in chosen for m in v["_members"]]

    # dati dei casi scelti: casi per attivita', passaggi diretti (ripetizioni consecutive raggruppate), inizi e fini
    act_cases, edge_cases, starts, ends = Counter(), Counter(), Counter(), Counter()
    for _, acts, _ in sel:
        if not acts:
            continue
        seq = [a for i, a in enumerate(acts) if i == 0 or acts[i - 1] != a]
        act_cases.update(set(seq))
        edge_cases.update({(a, b) for a, b in zip(seq, seq[1:])})
        starts[seq[0]] += 1
        ends[seq[-1]] += 1

    graph = _model_graph(xml)
    nodes = graph["nodes"]
    all_acts = {a for _, acts, _ in cases for a in acts}
    act_of = match_activities(graph, steps, labels, all_acts)
    START, END = "▶start", "■end"

    inc, out = defaultdict(list), defaultdict(list)
    for fid, s, t in graph["flows"]:
        out[s].append((fid, t))
        inc[t].append((fid, s))

    def back(nid, seen):
        """Attivita' (o START) da cui si arriva a nid passando solo per nodi che non sono attivita'."""
        if nid in act_of:
            return {act_of[nid]}
        if nodes.get(nid, {}).get("tag") == "startEvent":
            return {START}
        if nid in seen:
            return set()
        seen.add(nid)
        r = set()
        for _, s in inc.get(nid, ()):
            r |= back(s, seen)
        return r

    def fwd(nid, seen):
        if nid in act_of:
            return {act_of[nid]}
        if nodes.get(nid, {}).get("tag") == "endEvent":
            return {END}
        if nid in seen:
            return set()
        seen.add(nid)
        r = set()
        for _, t in out.get(nid, ()):
            r |= fwd(t, seen)
        return r

    def pair_count(a, b):
        if a == START:
            return starts.get(b, 0)
        if b == END:
            return ends.get(a, 0)
        return edge_cases.get((a, b), 0)

    flows, modeled = {}, set()
    for fid, s, t in graph["flows"]:
        pairs = {(a, b) for a in back(s, set()) for b in fwd(t, set())}
        modeled |= pairs
        cnt = sum(pair_count(a, b) for a, b in pairs)
        flows[fid] = {"status": "ok" if cnt else "miss", "count": cnt}

    elements = {}
    for nid, n in nodes.items():
        if nid in act_of:
            c = act_cases.get(act_of[nid], 0)
            elements[nid] = {"status": "ok" if c else "miss", "count": c, "activity": act_of[nid]}
        elif n["tag"] == "startEvent":
            c = sum(pair_count(START, b) for b in fwd(nid, set()))
            elements[nid] = {"status": "ok" if c else "miss", "count": c}
        elif n["tag"] == "endEvent":
            c = sum(pair_count(a, END) for a in back(nid, set()))
            elements[nid] = {"status": "ok" if c else "miss", "count": c}
        elif n["type"] in ("gateway", "event"):
            ins = [flows[f]["count"] for f, _ in inc.get(nid, ()) if f in flows]
            elements[nid] = {"status": "ok" if any(ins) else "miss", "count": sum(ins)}
        elif n["type"] in ("activity", "subprocess"):
            elements[nid] = {"status": "miss", "count": 0}   # task che non corrisponde a nessuna attivita' dei dati

    # in rosso: attivita' dei dati che il modello non ha, e passaggi tra attivita' del modello che il modello non ha
    in_model = set(act_of.values())
    extra = [a for a, _ in act_cases.most_common() if a not in in_model]
    shown = extra[:MAX_EXTRA]
    el_of = defaultdict(list)
    for nid, a in act_of.items():
        el_of[a].append(nid)

    def ends_of(a):
        return el_of[a][:1] if a in el_of else (["x:" + a] if a in shown else [])
    red = []
    for (a, b), c in edge_cases.most_common():
        if a in in_model and b in in_model and (a, b) in modeled:
            continue
        if (a in in_model or a in shown) and (b in in_model or b in shown):
            sa, sb = ends_of(a), ends_of(b)
            if sa and sb and sa[0] != sb[0]:
                red.append({"source": sa[0], "target": sb[0], "count": c})
        if len(red) >= MAX_EXTRA_EDGES:
            break
    tr = lambda a: (labels.get(a) or {}).get(lang) or a
    return {
        "cases_total": len(cases), "cases_selected": len(sel), "variants_total": len(vs),
        "picked": None if variant_ids is None else sorted(set(variant_ids)),
        "variants": [{k: v[k] for k in ("id", "steps", "count", "share", "median")} for v in vs[:MAX_VARIANTS]],
        "elements": elements, "flows": flows,
        "extra": [{"id": "x:" + a, "activity": a, "name": tr(a), "count": act_cases[a]} for a in shown],
        "extra_hidden": max(0, len(extra) - len(shown)),
        "red_edges": red,
        "unmatched_tasks": [nid for nid, n in nodes.items() if n["type"] in ("activity", "subprocess") and nid not in act_of],
    }
