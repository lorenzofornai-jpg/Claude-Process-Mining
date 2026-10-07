"""Process Overview: volumi, tempi e varianti di un dataset OCEL, visti da un oggetto guida.

Nell'object-centric non esiste un unico «caso»: ordini, righe, fatture hanno ognuno la
sua storia. Come le «perspectives» di Celonis e il «leading object type» di Adams et al.
(Defining Cases and Variants for Object-Centric Event Data, 2022), l'utente sceglie un
tipo di oggetto guida e la pagina si calcola su quello:

- volumi: quanti oggetti, quanti eventi, quanti oggetti iniziano ogni mese;
- tempi: dal primo all'ultimo evento dell'oggetto («solo l'oggetto») oppure includendo gli
  eventi degli oggetti collegati («con gli oggetti collegati»: l'ordine e' finito quando
  e' finito l'ultimo oggetto collegato, es. l'ultima fattura). I tipi trasversali
  (cliente, fornitore) non contano: collegherebbero documenti diversi;
- varianti: la sequenza delle attivita' dell'oggetto guida; le ripetizioni consecutive
  della stessa attivita' (es. tre entrate merci di fila) sono raggruppate in un passo «×»,
  cosi' le varianti non esplodono per il numero di ripetizioni (divergenza);
- happy path (la variante piu' frequente) e le altre attivita' frequenti fuori da esso.

In alto si vedono tutti i tipi di oggetto e, per il tipo guida, quanti oggetti di ogni
altro tipo ha collegati in media.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone

from app.services.explorer import ExplorerModel

MAX_VARIANTS = 300
HIST_BINS = 12


def lead_options(model: ExplorerModel) -> list[str]:
    """Tipi che possono fare da oggetto guida: con eventi propri e non trasversali."""
    return [t for t in model.types if model.with_events.get(t) and not model.is_hub(t)]


def default_lead(model: ExplorerModel, main_object: str | None = None) -> str | None:
    """L'oggetto principale dell'assessment se lo si riconosce nei nomi dei tipi, altrimenti il tipo con
    una storia (in media almeno 1,5 eventi per oggetto) con piu' oggetti, altrimenti il piu' numeroso."""
    options = lead_options(model)
    if not options:
        return None
    if main_object:
        words = {w for w in main_object.lower().replace("/", " ").split() if len(w) >= 4}
        scored = [(sum(1 for w in words if w in t.lower()), t) for t in options]
        scored = [x for x in scored if x[0]]
        if scored:
            return max(scored)[1]
    with_history = [t for t in options if model.avg_events(t) >= 1.5]
    return (with_history or options)[0]   # model.types e' gia' in ordine di numero di oggetti


def _finite(values):
    return [v for v in values if v is not None and not math.isnan(v)]


def _stats(values: list[float]) -> dict | None:
    values = _finite(values)
    if not values:
        return None
    values = sorted(values)
    p90 = values[min(len(values) - 1, int(round(0.9 * (len(values) - 1))))]
    return {"median": statistics.median(values), "mean": statistics.fmean(values),
            "min": values[0], "max": values[-1], "p90": p90, "n": len(values)}


def _histogram(values: list[float]) -> list[dict]:
    """Distribuzione dei tempi in giorni: classi di uguale ampiezza fino al 95° percentile, poi una
    classe finale «oltre» per non schiacciare il grafico con pochi casi lunghissimi."""
    values = sorted(_finite(values))
    if not values:
        return []
    days = [v / 86400 for v in values]
    top = days[min(len(days) - 1, int(0.95 * (len(days) - 1)))]
    if top <= 0:
        return [{"from": 0, "to": 0, "count": len(days), "overflow": False}]
    width = top / HIST_BINS
    bins = [{"from": i * width, "to": (i + 1) * width, "count": 0, "overflow": False} for i in range(HIST_BINS)]
    over = {"from": top, "to": days[-1], "count": 0, "overflow": True}
    for d in days:
        if d > top:
            over["count"] += 1
        else:
            bins[min(HIST_BINS - 1, int(d / width))]["count"] += 1
    return bins + ([over] if over["count"] else [])


def _steps(acts: list[str]) -> tuple:
    """Sequenza con le ripetizioni consecutive raggruppate: ((attivita', ripetuta?), ...)."""
    out = []
    for a in acts:
        if out and out[-1][0] == a:
            out[-1] = (a, True)
        else:
            out.append((a, False))
    return tuple(out)


def build_overview(model: ExplorerModel, lead: str, scope: str = "object") -> dict:
    hubs = {t for t in model.types if model.is_hub(t)}
    seqs = model.sequences_by_type.get(lead, [])

    # ---------- tutti i tipi di oggetto ----------
    def lifetime(seq):
        ts = _finite([x[0] for x in seq])
        return (ts[-1] - ts[0]) if ts else None
    types = []
    for t in model.types:
        lifetimes = [lifetime(seq) for _, seq in model.sequences_by_type.get(t, [])]
        st = _stats(lifetimes)
        types.append({"name": t, "color": model.colors[t], "objects": model.object_counts[t],
                      "with_events": model.with_events[t], "events": model.related_events[t],
                      "hub": model.is_hub(t), "lead_option": t in lead_options(model),
                      "median_lifetime": st["median"] if st else None})

    # ---------- oggetti collegati al tipo guida ----------
    related_by_obj: dict[str, set[str]] = {}
    per_type_counts: dict[str, list[int]] = defaultdict(list)
    for oid, seq in seqs:
        rel = set()
        for _, _, idx in seq:
            rel.update(model.event_objects[idx])
        rel.discard(oid)
        related_by_obj[oid] = rel
        c = Counter(model.type_of[r] for r in rel)
        for t in model.types:
            if t != lead:
                per_type_counts[t].append(c.get(t, 0))
    relations = []
    for t, counts in per_type_counts.items():
        with_any = sum(1 for c in counts if c)
        if with_any:
            relations.append({"type": t, "color": model.colors[t], "hub": t in hubs,
                              "avg": statistics.fmean(counts), "share": with_any / len(counts)})
    relations.sort(key=lambda r: (-r["share"], -r["avg"]))

    # ---------- tempi e volumi dell'oggetto guida ----------
    obj_events: dict[str, list[int]] = {}
    for oid, seq in seqs:
        obj_events[oid] = [idx for _, _, idx in seq]

    def throughput(oid, seq):
        if scope == "related":
            idxs = set(obj_events[oid])
            for r in related_by_obj[oid]:
                if model.type_of[r] in hubs:
                    continue
                for _, _, idx in model.sequences_by_type_index.get(r, ()):
                    idxs.add(idx)
            ts = _finite([model.event_times[i] for i in idxs])
            return (max(ts) - min(ts)) if ts else None
        return lifetime(seq)

    tps: dict[str, float | None] = {oid: throughput(oid, seq) for oid, seq in seqs}
    months: Counter = Counter()
    for oid, seq in seqs:
        ts = _finite([x[0] for x in seq])
        if ts:
            months[datetime.fromtimestamp(ts[0], tz=timezone.utc).strftime("%Y-%m")] += 1
    month_list = []
    if months:
        y, m = map(int, min(months).split("-"))
        last = max(months)
        while True:
            key = f"{y:04d}-{m:02d}"
            month_list.append({"month": key, "count": months.get(key, 0)})
            if key == last:
                break
            m += 1
            if m > 12:
                y, m = y + 1, 1

    # ---------- varianti ----------
    groups: dict[tuple, list[str]] = defaultdict(list)
    for oid, seq in seqs:
        groups[_steps([a for _, a, _ in seq])].append(oid)
    n_obj = len(seqs)
    variants = []
    cum = 0
    for steps, oids in sorted(groups.items(), key=lambda kv: (-len(kv[1]), len(kv[0]), kv[0])):
        cum += len(oids)
        st = _stats([tps[o] for o in oids])
        variants.append({"steps": [{"activity": a, "repeated": r} for a, r in steps],
                         "count": len(oids), "share": len(oids) / n_obj, "cum_share": cum / n_obj,
                         "median": st["median"] if st else None, "mean": st["mean"] if st else None})
    happy = variants[0] if variants else None

    # ---------- altre attivita' frequenti ----------
    happy_acts = {s["activity"] for s in happy["steps"]} if happy else set()
    act_objs: dict[str, set] = defaultdict(set)
    act_events: Counter = Counter()
    for oid, seq in seqs:
        for _, a, _ in seq:
            act_objs[a].add(oid)
            act_events[a] += 1
    others = sorted(({"activity": a, "objects": len(o), "share": len(o) / n_obj, "events": act_events[a]}
                     for a, o in act_objs.items() if a not in happy_acts), key=lambda x: -x["objects"])

    all_tp = [tps[o] for o, _ in seqs]
    return {
        "lead": lead, "scope": scope, "lead_options": lead_options(model),
        "hub_types": [t for t in model.types if t in hubs],
        "types": types, "relations": relations,
        # eventi distinti (un evento condiviso da piu' oggetti conta una volta); per oggetto: media degli eventi di ognuno
        "volumes": {"objects": n_obj, "events": len({idx for _, s in seqs for _, _, idx in s}),
                    "events_per_object": (sum(len(s) for _, s in seqs) / n_obj) if n_obj else 0,
                    "months": month_list},
        "throughput": {"stats": _stats(all_tp), "histogram": _histogram(all_tp)},
        "variants": variants[:MAX_VARIANTS], "variants_total": len(variants),
        "happy_path": happy,
        "other_activities": others[:12],
    }
