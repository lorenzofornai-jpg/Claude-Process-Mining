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

_CACHE: dict[tuple, "ExplorerModel"] = {}
_CACHE_MAX = 6


def _epoch(raw: str) -> float:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return float("nan")


class ExplorerModel:
    """Il log ridotto a cio' che serve al grafo: per ogni oggetto la sequenza (data, attivita', evento)."""

    def __init__(self, ocel: dict, colors: dict[str, str] | None = None, hubs: set[str] | None = None):
        """colors, hubs: quelli del dataset intero, per un modello filtrato (stessi colori e stessi tipi
        trasversali anche quando restano pochi oggetti)."""
        self._hubs = hubs
        type_of = {o["id"]: o["type"] for o in ocel.get("objects", [])}
        self.type_of = type_of
        # attributi degli oggetti (ultimo valore), per filtri come «tipo documento = fattura»
        self.object_attrs: dict[str, dict[str, str]] = {
            o["id"]: {a.get("name"): str(a.get("value")) for a in o.get("attributes") or [] if a.get("name")}
            for o in ocel.get("objects", [])}
        self.object_counts: dict[str, int] = defaultdict(int)
        for t in type_of.values():
            self.object_counts[t] += 1

        sequences: dict[str, list[tuple[float, int, str]]] = defaultdict(list)
        # (tipo, attivita') -> eventi e oggetti; attivita' -> eventi per tipo
        self.events_by_type_act: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.objects_by_type_act: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.event_count = 0
        relations: list[list[tuple[str, str, str]]] = []   # per evento: (oggetto, tipo, qualificatore)
        self.event_times: list[float] = []
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
            self.event_times.append(ts)

        # sequenze per tipo, ordinate per data; a parita' di data vale l'ordine nel file (come pm4py)
        self.sequences_by_type: dict[str, list[tuple[str, list[tuple[float, str, int]]]]] = defaultdict(list)
        for oid, seq in sequences.items():
            seq.sort(key=lambda x: (math.inf if math.isnan(x[0]) else x[0], x[1]))
            self.sequences_by_type[type_of[oid]].append((oid, [(ts, act, idx) for ts, idx, act in seq]))
        # sequenza di ogni oggetto, per id (serve all'overview)
        self.sequences_by_type_index = {oid: seq for t in self.sequences_by_type.values() for oid, seq in t}

        self.types = sorted(self.object_counts, key=lambda t: (-self.object_counts[t], t))
        self.colors = {t: (colors or {}).get(t) or PALETTE[i % len(PALETTE)] for i, t in enumerate(self.types)}
        self.with_events = {t: len(self.sequences_by_type.get(t, [])) for t in self.types}
        self.related_events = {t: len(set().union(*[e for (tt, _), e in self.events_by_type_act.items() if tt == t]))
                               for t in self.types}

        # Eventi «propri» dei tipi trasversali: quelli di cui il tipo e' l'oggetto di casa (qualificatore
        # «involves»), oppure che non fanno parte della storia di un altro oggetto con piu' passaggi (es. una
        # fattura). Per il cliente: cambio di rischio, blocco solleciti...; non la registrazione della fattura,
        # che appartiene alla storia della fattura e in cui il cliente e' solo citato. Un oggetto ha una storia
        # se ha almeno due eventi o se gli oggetti del suo tipo ne hanno di solito piu' di uno (una fattura
        # appena registrata ha un solo evento, ma e' comunque l'inizio della storia di un documento).
        # per evento: gli oggetti collegati (serve all'overview per i tempi «con gli oggetti collegati»)
        self.event_objects: list[list[str]] = [[oid for oid, _, _ in rels] for rels in relations]
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
        if self._hubs is not None:
            return t in self._hubs
        return self.avg_events(t) >= 8 and self.with_events[t] * 4 <= self.related_events[t]

    def default_types(self, preferred: list[str] | None = None) -> list[str]:
        """Selezione iniziale. preferred: l'oggetto guida e gli altri oggetti inclusi prima del mapping
        (quelli con eventi, al massimo quattro). Senza: i due tipi con piu' eventi collegati, esclusi i
        trasversali, cosi' il primo grafo e' leggibile."""
        chosen = [t for t in dict.fromkeys(preferred or []) if t in self.types and self.related_events.get(t)]
        if chosen:
            return chosen[:4]
        ranked = sorted((t for t in self.types if self.related_events[t]), key=lambda t: (-self.related_events[t], t))
        return ([t for t in ranked if not self.is_hub(t)] or ranked)[:2]

    def case_objects(self, oid: str, with_types: set[str], hops: int = 2) -> set[str]:
        """Il «caso» di un oggetto: lui e gli oggetti dei tipi with_types collegati a lui da eventi (fino a `hops`
        passaggi: riga d'ordine -> fattura -> pagamento). Non si passa per i tipi trasversali ne' per altri
        oggetti del suo tipo (le righe sorelle dello stesso ordine non entrano nel caso)."""
        own_type = self.type_of[oid]
        allowed = {t for t in with_types if t != own_type and not self.is_hub(t)}
        seen, frontier = {oid}, {oid}
        for _ in range(hops):
            nxt = set()
            for x in frontier:
                for _, _, idx in self.sequences_by_type_index.get(x, ()):
                    for y in self.event_objects[idx]:
                        if y not in seen and self.type_of.get(y) in allowed:
                            seen.add(y)
                            nxt.add(y)
            frontier = nxt
            if not frontier:
                break
        return seen

    def variants(self, t: str, with_types: list[str] | None = None) -> list[dict]:
        """Varianti di un tipo di oggetto, dalla piu' frequente. Senza with_types: la sequenza delle sue attivita'.
        Con with_types (gli altri tipi scelti nel grafo): la sequenza delle attivita' del suo caso, cioe' anche
        degli oggetti collegati di quei tipi, in ordine di tempo. Ripetizioni consecutive raggruppate, come nella
        Process Overview. id = posizione (1 = la piu' frequente); count = oggetti del tipo t."""
        others = tuple(sorted(set(with_types or []) - {t}))
        key = (t, others)
        cache = self.__dict__.setdefault("_variants", {})
        if key not in cache:
            groups: dict[tuple, list] = defaultdict(list)
            for oid, seq in self.sequences_by_type.get(t, []):
                if others:
                    events = {}
                    for x in self.case_objects(oid, set(others)):
                        for ts, a, idx in self.sequences_by_type_index.get(x, ()):
                            events[idx] = (ts, a, idx)
                    seq = sorted(events.values(), key=lambda e: (math.inf if math.isnan(e[0]) else e[0], e[2]))
                steps = tuple(a for i, (_, a, _) in enumerate(seq) if i == 0 or seq[i - 1][1] != a)
                ts = [x for x, _, _ in seq if not math.isnan(x)]
                groups[steps].append((oid, (ts[-1] - ts[0]) if ts else float("nan")))
            n = sum(len(v) for v in groups.values()) or 1
            out, of = [], {}
            for i, (steps, objs) in enumerate(sorted(groups.items(), key=lambda kv: (-len(kv[1]), len(kv[0]), kv[0])), 1):
                durs = [d for _, d in objs if not math.isnan(d)]
                out.append({"id": i, "steps": list(steps), "count": len(objs), "share": len(objs) / n,
                            "median": statistics.median(durs) if durs else None})
                of.update({oid: i for oid, _ in objs})
            cache[key] = (out, of)
        return cache[key][0]

    def variant_of(self, t: str, with_types: list[str] | None = None) -> dict[str, int]:
        self.variants(t, with_types)
        return self.__dict__["_variants"][(t, tuple(sorted(set(with_types or []) - {t})))][1]

    def type_summary(self) -> list[dict]:
        return [{"name": t, "color": self.colors[t], "objects": self.object_counts[t],
                 "with_events": self.with_events[t], "events": self.related_events[t],
                 "hub": self.is_hub(t), "avg": round(self.avg_events(t), 1),
                 "own_activities": self.own_activities(t) if self.is_hub(t) else []} for t in self.types]

    def own_activities(self, t: str) -> list[str]:
        """Attivita' degli eventi propri di un tipo trasversale (vuoto per gli altri tipi)."""
        own = self.own_events.get(t, set())
        return sorted({act for _, seq in self.sequences_by_type.get(t, []) for _, act, idx in seq if idx in own})


def load_model(path: str | Path, filters: list[dict] | None = None) -> ExplorerModel:
    """Modello del dataset, eventualmente filtrato per attributo (ocel_filter): in cache per file e filtri."""
    from app.services import ocel_filter
    filters = ocel_filter.clean(filters)
    if filters:
        base = load_model(path)
        path = Path(path)
        key = (str(path.resolve()), path.stat().st_mtime, json.dumps(filters, sort_keys=True))
        model = _CACHE.get(key)
        if model is None:
            hubs = {t for t in base.types if base.is_hub(t)}
            model = ExplorerModel(ocel_filter.apply(ocel_filter.load_raw(path), filters, hubs), base.colors, hubs)
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[key] = model
        return model
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
                top: int | None = None, paths: int = 100, hub_mode: str = "own",
                variant_type: str | None = None, variant_ids: list[int] | None = None) -> dict:
    """Grafo per i tipi scelti.

    Attivita' visibili: l'elenco `activities` se dato (scelta manuale), altrimenti le `top` piu' frequenti
    (tutte se `top` e' None). Si restituiscono tutti i collegamenti tra le attivita' visibili, con `kept`
    vero per quelli che rientrano nel `paths`% piu' frequente: il browser puo' poi mostrarne o
    nasconderne altri a mano senza ricalcolare.

    hub_mode="own": la linea di un tipo trasversale passa solo dai suoi eventi propri (vedi ExplorerModel);
    "all": da tutti gli eventi a cui il tipo e' collegato.
    """
    types = [t for t in (types if types is not None else model.default_types()) if t in model.object_counts]

    # filtro per varianti (alternativo a quello per attivita'): solo gli oggetti del tipo scelto che seguono le
    # varianti scelte, con tutte le loro attivita'
    seqs_of = model.sequences_by_type
    ev_by = model.events_by_type_act
    obj_by = model.objects_by_type_act
    if variant_type in model.object_counts:
        # varianti del caso: gli oggetti del tipo scelto nelle varianti spuntate e, per gli altri tipi scelti, gli
        # oggetti del loro caso (collegati a loro), ognuno con tutta la sua storia
        others = [t for t in types if t != variant_type and not model.is_hub(t)]
        allowed = set(variant_ids or [])
        of = model.variant_of(variant_type, others)
        leads = [oid for oid, _ in model.sequences_by_type.get(variant_type, []) if of.get(oid) in allowed]
        members: set[str] = set()
        for oid in leads:
            members |= model.case_objects(oid, set(others))
        types = [variant_type] + others
        seqs_of = {t: [(oid, seq) for oid, seq in model.sequences_by_type.get(t, []) if oid in members] for t in types}
        ev_by, obj_by = defaultdict(set), defaultdict(set)
        for t in types:
            for oid, seq in seqs_of[t]:
                for _, act, idx in seq:
                    ev_by[(t, act)].add(idx)
                    obj_by[(t, act)].add(oid)
        activities = None
        top = None

    # attivita' dei tipi scelti, per numero di eventi (un evento con piu' tipi conta una volta)
    act_events: dict[str, set[str]] = defaultdict(set)
    for (t, act), ev_ids in ev_by.items():
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
        for oid, seq in seqs_of.get(t, []):
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
                     "events": len(ev_by.get((t, act), ())),
                     "objects": len(obj_by.get((t, act), ()))}
                    for t in types if ev_by.get((t, act))]
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
        "variants": ({"type": variant_type, "ids": sorted(set(variant_ids or [])),
                      "objects": len(seqs_of.get(variant_type, [])),
                      "with_types": [t for t in types if t != variant_type]} if variant_type in model.object_counts else None),
        "own_activities": {t: model.own_activities(t) for t in types if t in model.own_events},
        "activity_list": [{"name": a, "events": len(act_events[a]), "selected": a in kept} for a in ranking],
        "activities": {"shown": len(kept), "total": len(ranking)},
        "paths": {"percent": paths, "shown": len(keep_edges), "total": len(all_edges)},
        "nodes": nodes, "edges": edges,
    }
