"""Process Explorer: grafo dei flussi object-centric (OC-DFG) di un dataset OCEL 2.0.

Per ogni tipo di oggetto si ordinano per data gli eventi di ciascun oggetto e
si contano i passaggi da un'attivita' alla successiva («percorsi»). Ogni tipo
di oggetto ha il suo colore, il suo inizio e la sua fine; un'attivita'
condivisa da piu' tipi (es. «Crea ordine» per ordine e posizioni) e' un solo
nodo in cui si incrociano i flussi.

Il browser riceve solo il grafo gia' aggregato, mai il log: i filtri
(tipi di oggetto, numero di attivita', quota di percorsi) si ricalcolano qui.

Filtri:
- attivita': si tengono le N piu' frequenti; le altre si tolgono dalla storia
  di ogni oggetto e i passaggi si ricollegano (A→B→C senza B diventa A→C);
- percorsi: si tiene la quota piu' frequente dei collegamenti, ma ogni
  attivita' visibile conserva sempre il suo collegamento in entrata e in
  uscita piu' frequente, cosi' il grafo resta leggibile anche al minimo.

La lettura del file e' in cache (per percorso e data di modifica).
"""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Colori distinti e leggibili sia su fondo chiaro sia scuro, assegnati ai tipi in ordine di numero di oggetti.
PALETTE = ["#3b63e0", "#e07b39", "#1b9e77", "#c2408f", "#8c6bd6", "#c9a227", "#2b9fbf", "#d1495b",
           "#5c8a2e", "#7a6a58", "#e05fb0", "#4a5a78"]

START = "__start__"
END = "__end__"

_CACHE: dict[tuple[str, float], "ExplorerModel"] = {}
_CACHE_MAX = 4


def _epoch(raw: str) -> float:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return float("nan")


class ExplorerModel:
    """Il log ridotto a cio' che serve al grafo: per ogni oggetto la sequenza (data, attivita', evento)."""

    def __init__(self, ocel: dict):
        type_of = {o["id"]: o["type"] for o in ocel.get("objects", [])}
        self.object_counts: dict[str, int] = defaultdict(int)
        for t in type_of.values():
            self.object_counts[t] += 1

        sequences: dict[str, list[tuple[float, int, str]]] = defaultdict(list)
        # (tipo, attivita') -> eventi e oggetti; attivita' -> eventi per tipo
        self.events_by_type_act: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.objects_by_type_act: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.event_count = 0
        relations: list[list[tuple[str, str, str]]] = []   # per evento: (oggetto, tipo, qualificatore)
        for idx, ev in enumerate(ocel.get("events", [])):
            self.event_count += 1
            ts = _epoch(ev.get("time", ""))
            act = ev.get("type", "")
            seen = set()
            rels = []
            for rel in ev.get("relationships", []):
                oid = rel.get("objectId")
                otype = type_of.get(oid)
                if otype is None or oid in seen:
                    continue
                seen.add(oid)
                rels.append((oid, otype, rel.get("qualifier") or ""))
                sequences[oid].append((ts, idx, act))
                self.events_by_type_act[(otype, act)].add(ev.get("id", ""))
                self.objects_by_type_act[(otype, act)].add(oid)
            relations.append(rels)

        # sequenze per tipo, ordinate per data; a parita' di data vale l'ordine nel file (come pm4py)
        self.sequences_by_type: dict[str, list[tuple[str, list[tuple[float, str, int]]]]] = defaultdict(list)
        for oid, seq in sequences.items():
            seq.sort(key=lambda x: (math.inf if math.isnan(x[0]) else x[0], x[1]))
            self.sequences_by_type[type_of[oid]].append((oid, [(ts, act, idx) for ts, idx, act in seq]))

        self.types = sorted(self.object_counts, key=lambda t: (-self.object_counts[t], t))
        self.colors = {t: PALETTE[i % len(PALETTE)] for i, t in enumerate(self.types)}
        self.with_events = {t: len(self.sequences_by_type.get(t, [])) for t in self.types}
        self.related_events = {t: len(set().union(*[e for (tt, _), e in self.events_by_type_act.items() if tt == t]))
                               for t in self.types}

        # Eventi «propri» dei tipi trasversali: quelli di cui il tipo e' l'oggetto di casa (qualificatore
        # «involves»), oppure che non fanno parte della storia di un altro oggetto con piu' passaggi (es. una
        # fattura). Per il cliente: cambio di rischio, blocco solleciti...; non la registrazione della fattura,
        # che appartiene alla storia della fattura e in cui il cliente e' solo citato. Un oggetto ha una storia
        # se ha almeno due eventi o se gli oggetti del suo tipo ne hanno di solito piu' di uno (una fattura
        # appena registrata ha un solo evento, ma e' comunque l'inizio della storia di un documento).
        hubs = {t for t in self.types if self.is_hub(t)}
        n_events = {oid: len(seq) for oid, seq in sequences.items()}
        lifecycle_types = {t for t in self.types if t not in hubs and self.avg_events(t) >= 1.5}
        self.own_events: dict[str, set[int]] = {t: set() for t in hubs}
        for idx, rels in enumerate(relations):
            in_history = any(otype not in hubs and (otype in lifecycle_types or n_events.get(oid, 0) >= 2)
                             for oid, otype, _ in rels)
            for oid, otype, qual in rels:
                if otype in hubs and (qual == "involves" or not in_history):
                    self.own_events[otype].add(idx)

    def avg_events(self, t: str) -> float:
        return self.related_events[t] / self.with_events[t] if self.with_events.get(t) else 0.0

    def is_hub(self, t: str) -> bool:
        """Tipo «trasversale» (cliente, fornitore, materiale...): pochi oggetti, ognuno in moltissimi eventi di
        documenti diversi. La sua linea collega eventi di documenti diversi e attraversa tutto il grafo."""
        return self.avg_events(t) >= 8 and self.with_events[t] * 4 <= self.related_events[t]

    def default_types(self) -> list[str]:
        """Selezione iniziale: i (massimo) due tipi con piu' eventi collegati, esclusi i trasversali, cosi' il
        primo grafo e' leggibile."""
        ranked = sorted((t for t in self.types if self.related_events[t]), key=lambda t: (-self.related_events[t], t))
        return ([t for t in ranked if not self.is_hub(t)] or ranked)[:2]

    def type_summary(self) -> list[dict]:
        return [{"name": t, "color": self.colors[t], "objects": self.object_counts[t],
                 "with_events": self.with_events[t], "events": self.related_events[t],
                 "hub": self.is_hub(t), "avg": round(self.avg_events(t), 1),
                 "own_activities": self.own_activities(t) if self.is_hub(t) else []} for t in self.types]

    def own_activities(self, t: str) -> list[str]:
        """Attivita' degli eventi propri di un tipo trasversale (vuoto per gli altri tipi)."""
        own = self.own_events.get(t, set())
        return sorted({act for _, seq in self.sequences_by_type.get(t, []) for _, act, idx in seq if idx in own})


def load_model(path: str | Path) -> ExplorerModel:
    path = Path(path)
    key = (str(path.resolve()), path.stat().st_mtime)
    model = _CACHE.get(key)
    if model is None:
        with open(path, encoding="utf-8") as f:
            model = ExplorerModel(json.load(f))
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = model
    return model


def _duration_stats(values: list[float]) -> dict | None:
    values = [v for v in values if not math.isnan(v)]
    if not values:
        return None
    return {"median": statistics.median(values), "mean": statistics.fmean(values),
            "min": min(values), "max": max(values)}


def build_graph(model: ExplorerModel, types: list[str] | None = None, activities: list[str] | None = None,
                top: int | None = None, paths: int = 100, hub_mode: str = "own") -> dict:
    """Grafo per i tipi scelti.

    Attivita' visibili: l'elenco `activities` se dato (scelta manuale), altrimenti le `top` piu' frequenti
    (tutte se `top` e' None). Si restituiscono tutti i collegamenti tra le attivita' visibili, con `kept`
    vero per quelli che rientrano nel `paths`% piu' frequente: il browser puo' poi mostrarne o
    nasconderne altri a mano senza ricalcolare.

    hub_mode="own": la linea di un tipo trasversale passa solo dai suoi eventi propri (vedi ExplorerModel);
    "all": da tutti gli eventi a cui il tipo e' collegato.
    """
    types = [t for t in (types if types is not None else model.default_types()) if t in model.object_counts]

    # attivita' dei tipi scelti, per numero di eventi (un evento con piu' tipi conta una volta)
    act_events: dict[str, set[str]] = defaultdict(set)
    for (t, act), ev_ids in model.events_by_type_act.items():
        if t in types:
            act_events[act] |= ev_ids
    ranking = sorted(act_events, key=lambda a: (-len(act_events[a]), a))
    if activities is not None:
        kept = set(activities) & set(ranking)
    else:
        n = len(ranking) if top is None else max(1, int(top))
        kept = set(ranking[:n])

    # collegamenti per tipo: (tipo, da, a) -> passaggi, oggetti, durate
    count: dict[tuple, int] = defaultdict(int)
    objs: dict[tuple, set] = defaultdict(set)
    durs: dict[tuple, list] = defaultdict(list)
    for t in types:
        own = model.own_events.get(t) if hub_mode == "own" else None
        for oid, seq in model.sequences_by_type.get(t, []):
            seq = [(ts, a) for ts, a, idx in seq if a in kept and (own is None or idx in own)]
            if not seq:
                continue
            steps = [(None, START)] + seq + [(None, END)]
            for (ts1, a1), (ts2, a2) in zip(steps, steps[1:]):
                src = f"{START}|{t}" if a1 == START else a1
                tgt = f"{END}|{t}" if a2 == END else a2
                key = (t, src, tgt)
                count[key] += 1
                objs[key].add(oid)
                if ts1 is not None and ts2 is not None:
                    durs[key].append(ts2 - ts1)

    # filtro dei percorsi: la quota piu' frequente, piu' per ogni attivita' il collegamento migliore in entrata e in uscita
    all_edges = sorted(count, key=lambda k: (-count[k], k))
    paths = max(0, min(int(paths), 100))
    keep_edges = set(all_edges[:math.ceil(len(all_edges) * paths / 100)])
    best_in: dict[tuple, tuple] = {}
    best_out: dict[tuple, tuple] = {}
    for k in all_edges:  # gia' in ordine di frequenza: il primo visto e' il migliore
        t, src, tgt = k
        best_out.setdefault((t, src), k)
        best_in.setdefault((t, tgt), k)
    keep_edges |= set(best_in.values()) | set(best_out.values())

    edges = [{
        "id": f"e{i}", "type": t, "color": model.colors[t], "source": src, "target": tgt, "kept": k in keep_edges,
        "count": count[k], "objects": len(objs[k]), "duration": _duration_stats(durs[k]),
    } for i, k in enumerate(all_edges) for t, src, tgt in [k]]
    used_nodes = {e["source"] for e in edges} | {e["target"] for e in edges}

    nodes = []
    for act in ranking:
        if act not in kept or act not in used_nodes:
            continue
        per_type = [{"type": t, "color": model.colors[t],
                     "events": len(model.events_by_type_act.get((t, act), ())),
                     "objects": len(model.objects_by_type_act.get((t, act), ()))}
                    for t in types if model.events_by_type_act.get((t, act))]
        nodes.append({"id": act, "kind": "activity", "label": act, "count": len(act_events[act]), "types": per_type})
    for t in types:
        for kind, prefix in (("start", START), ("end", END)):
            node_id = f"{prefix}|{t}"
            if node_id in used_nodes:
                n_obj = sum(count[k] for k in all_edges if (k[1] if kind == "start" else k[2]) == node_id)
                nodes.append({"id": node_id, "kind": kind, "label": t, "type": t,
                              "color": model.colors[t], "count": n_obj})

    return {
        "types": types, "hub_mode": hub_mode,
        "own_activities": {t: model.own_activities(t) for t in types if t in model.own_events},
        "activity_list": [{"name": a, "events": len(act_events[a]), "selected": a in kept} for a in ranking],
        "activities": {"shown": len(kept), "total": len(ranking)},
        "paths": {"percent": paths, "shown": len(keep_edges), "total": len(all_edges)},
        "nodes": nodes, "edges": edges,
    }
