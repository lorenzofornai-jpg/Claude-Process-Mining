"""Obiettivo misurabile: dalla domanda di business al tempo «da inizio a fine» su un oggetto.

L'assessment raccoglie in linguaggio di business oggetto principale, evento di inizio ed
evento di fine (es. «fattura cliente», «emissione», «incasso»). Qui diventano un obiettivo
legato al dataset:

- tipo di oggetto da misurare (es. il documento contabile);
- filtro facoltativo su un attributo (es. tipo documento = fattura, quando lo stesso tipo
  contiene anche documenti di altra natura come i pagamenti);
- attivita' di inizio e di fine.

Il controllo dice su quanti oggetti l'obiettivo e' davvero misurabile e, quando non lo e',
perche' e cosa fare. Le regole sono generiche, non legate a SAP:

- inizio e fine su tipi di oggetto diversi → manca un collegamento evento→oggetto;
- molti oggetti senza inizio ne' fine → il tipo mescola oggetti di natura diversa; se una
  colonna li distingue (es. BLART in SAP, doc_type altrove) si propone il filtro;
- oggetti iniziati e non finiti → pratiche aperte, con la loro eta';
- fine senza inizio o prima dell'inizio → storico parziale o collegamento sbagliato.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict

from app.i18n import count, msg, render
from app.services.explorer import ExplorerModel

MAX_FILTER_VALUES = 12


def goal_texts(answers: dict) -> dict:
    """Le risposte dell'assessment che descrivono l'obiettivo, cosi' come scritte."""
    return {k: answers.get(k) for k in ("main_object", "start_event", "end_event", "key_questions", "kpis", "objectives")
            if answers.get(k)}


def _candidate_types(model: ExplorerModel) -> list[str]:
    return [t for t in model.types if model.with_events.get(t) and not model.is_hub(t)]


def _attributes(model: ExplorerModel, t: str) -> list[dict]:
    """Attributi «di categoria» del tipo: presenti su almeno meta' degli oggetti, con 2..12 valori."""
    oids = [oid for oid, _ in model.sequences_by_type.get(t, [])]
    values: dict[str, Counter] = defaultdict(Counter)
    for oid in oids:
        for name, v in model.object_attrs.get(oid, {}).items():
            values[name][v] += 1
    out = []
    for name, c in values.items():
        if 2 <= len(c) <= MAX_FILTER_VALUES and sum(c.values()) >= len(oids) / 2:
            out.append({"name": name, "values": [{"value": v, "count": n} for v, n in c.most_common()]})
    return out


def options(model: ExplorerModel) -> dict:
    """Cosa si puo' scegliere: tipi, loro attivita' (con quanti oggetti) e attributi di categoria."""
    types = []
    for t in _candidate_types(model):
        acts = sorted(((a, len(o)) for (tt, a), o in model.objects_by_type_act.items() if tt == t), key=lambda x: -x[1])
        types.append({"name": t, "objects": model.with_events[t],
                      "activities": [{"name": a, "objects": n} for a, n in acts],
                      "attributes": _attributes(model, t)})
    return {"types": types}


def _words(text: str | None) -> set[str]:
    return {w for w in (text or "").lower().replace("/", " ").replace("-", " ").split() if len(w) >= 4}


def propose(model: ExplorerModel, answers: dict) -> dict | None:
    """Prima proposta: tipo che somiglia all'oggetto principale dell'assessment (altrimenti quello con
    piu' oggetti dalla storia di almeno due attivita'), inizio = prima attivita' piu' frequente,
    fine = ultima attivita' piu' frequente; poi il filtro suggerito dal controllo, se c'e'."""
    cands = _candidate_types(model)
    if not cands:
        return None
    words = _words(answers.get("main_object"))

    def multi(t):
        return sum(1 for _, seq in model.sequences_by_type.get(t, []) if len({a for _, a, _ in seq}) >= 2)
    by_name = [(sum(1 for w in words if w in t.lower()), t) for t in cands]
    by_name = [x for x in by_name if x[0]]
    if answers.get("main_object") in cands:  # oggetto guida confermato prima del mapping
        t = answers["main_object"]
    else:
        t = max(by_name)[1] if by_name else max(cands, key=lambda c: (multi(c), model.with_events[c]))

    seqs = model.sequences_by_type.get(t, [])
    firsts = Counter(seq[0][1] for _, seq in seqs if seq)
    lasts = Counter(seq[-1][1] for _, seq in seqs if len({a for _, a, _ in seq}) >= 2)

    def match(text, counter):
        w = _words(text)
        scored = [(sum(1 for x in w if x in a.lower()), n, a) for a, n in counter.items()]
        scored = [x for x in scored if x[0]]
        return max(scored)[2] if scored else None
    acts = Counter({a: len(o) for (tt, a), o in model.objects_by_type_act.items() if tt == t})
    start = match(answers.get("start_event"), acts) or (firsts.most_common(1)[0][0] if firsts else None)
    end = match(answers.get("end_event"), acts) or (lasts.most_common(1)[0][0] if lasts else None)
    if end is None or end == start:
        others = [a for a, _ in acts.most_common() if a != start]
        end = others[0] if others else start
    binding = {"object_type": t, "filter_attribute": None, "filter_values": None,
               "start_activity": start, "end_activity": end}
    suggestion = check(model, binding).get("suggested_filter")
    if suggestion:
        binding.update(suggestion)
    return binding


def _finite(x):
    return x is not None and not math.isnan(x)


def check(model: ExplorerModel, b: dict) -> dict:
    """Su quanti oggetti l'obiettivo e' misurabile, con tempi, pratiche aperte e problemi (msg da tradurre)."""
    t, s, e = b.get("object_type"), b.get("start_activity"), b.get("end_activity")
    fa, fv = b.get("filter_attribute"), set(b.get("filter_values") or [])
    seqs = [(oid, seq) for oid, seq in model.sequences_by_type.get(t, [])
            if not fa or model.object_attrs.get(oid, {}).get(fa) in fv]
    times = [x for x in model.event_times if _finite(x)]
    data_end = max(times) if times else None

    both, open_, end_only, end_before, neither = [], [], 0, 0, 0
    has_start_by_obj = {}
    for oid, seq in seqs:
        ts_start = next((ts for ts, a, _ in seq if a == s), None)
        has_start_by_obj[oid] = ts_start is not None
        ends = [ts for ts, a, _ in seq if a == e]
        if ts_start is None:
            if ends:
                end_only += 1
            else:
                neither += 1
            continue
        after = [x for x in ends if _finite(x) and _finite(ts_start) and x >= ts_start]
        if after:
            both.append(after[0] - ts_start)
        elif ends:
            end_before += 1
        else:
            open_.append((data_end - ts_start) if data_end is not None and _finite(ts_start) else None)

    n = len(seqs)
    res = {"objects": n, "measurable": len(both), "open": len(open_), "end_only": end_only,
           "end_before_start": end_before, "neither": neither,
           "median": statistics.median(both) if both else None,
           "p90": sorted(both)[min(len(both) - 1, int(round(0.9 * (len(both) - 1))))] if both else None,
           "open_median_age": statistics.median([x for x in open_ if x is not None]) if any(x is not None for x in open_) else None,
           "issues": [], "suggested_filter": None}
    issues = res["issues"]
    if not t or not s or not e:
        issues.append({"level": "error", "text": msg("Scegli il tipo di oggetto e le attività di inizio e di fine.")})
        return res
    if s == e:
        issues.append({"level": "error", "text": msg("Inizio e fine sono la stessa attività: scegli due attività diverse.")})
        res.update(measurable=0, open=0, end_only=0, end_before_start=0, median=None, p90=None, open_median_age=None)
        return res

    if not both:
        types_of = lambda act: sorted({tt for (tt, a) in model.events_by_type_act if a == act})
        for role, act in (("start", s), ("end", e)):
            on = types_of(act)
            if t not in on:
                others = ", ".join(on) or "—"
                issues.append({"level": "error", "text": msg(
                    "L'attività «{a}» non è mai collegata a {t}: compare solo su {o}. Nella revisione del mapping usa «Aggiungi collegamento» sull'evento di «{a}» per collegarlo a {t} tramite una colonna in comune (es. il numero del documento).",
                    a=act, t=t, o=others)})
        if not issues:
            issues.append({"level": "error", "text": msg(
                "Nessun oggetto di tipo {t} ha sia «{s}» sia «{e}»: inizio e fine sono collegati a oggetti diversi dello stesso tipo. Controlla che la fine sia collegata allo stesso oggetto dell'inizio (es. alla fattura e non al documento di pagamento).",
                t=t, s=s, e=e)})

    # tipo che mescola oggetti di natura diversa: c'e' una colonna che separa chi ha l'inizio da chi no?
    if n and neither / n >= 0.2:
        suggestion = None if fa else _split(model, seqs, has_start_by_obj)
        if suggestion:
            res["suggested_filter"] = suggestion
            issues.append({"level": "warning", "suggest": suggestion, "text": msg(
                "Il {p} degli oggetti di tipo {t} non ha né «{s}» né «{e}»: il tipo mescola oggetti di natura diversa. La colonna {a} li distingue: l'inizio c'è solo per {a} = {v}. Conviene misurare solo quelli.",
                p=f"{round(100 * neither / n)}%", t=t, s=s, e=e, a=suggestion["filter_attribute"],
                v=", ".join(suggestion["filter_values"]))})
        else:
            issues.append({"level": "warning", "text": msg(
                "Il {p} degli oggetti di tipo {t} non ha né «{s}» né «{e}»: non attraversa il processo misurato e resta fuori dai tempi.",
                p=f"{round(100 * neither / n)}%", t=t, s=s, e=e)})
    if open_ and both:
        issues.append({"level": "info", "text": msg(
            "{c} ha l'inizio ma non ancora la fine: nell'analisi compare tra le pratiche aperte, con la sua età all'ultimo evento del dataset."
            if len(open_) == 1 else
            "{c} hanno l'inizio ma non ancora la fine: nell'analisi compaiono tra le pratiche aperte, con la loro età all'ultimo evento del dataset.",
            c=count(len(open_), t))})
    if end_only:
        issues.append({"level": "warning", "text": msg(
            "{c} ha «{e}» senza «{s}»: lo storico inizia a metà o l'inizio è collegato a un altro oggetto." if end_only == 1 else
            "{c} hanno «{e}» senza «{s}»: lo storico inizia a metà o l'inizio è collegato a un altro oggetto.",
            c=count(end_only, t), e=e, s=s)})
    if end_before:
        issues.append({"level": "warning", "text": msg(
            "{c} ha «{e}» solo prima di «{s}»: controlla le date o la scelta di inizio e fine." if end_before == 1 else
            "{c} hanno «{e}» solo prima di «{s}»: controlla le date o la scelta di inizio e fine.",
            c=count(end_before, t), e=e, s=s)})
    if both:
        issues.insert(0, {"level": "ok", "text": msg(
            "Obiettivo misurabile per {c} su {m}.", c=count(len(both), t), m=n)})
    return res


def _split(model: ExplorerModel, seqs, has_start: dict) -> dict | None:
    """Colonna di categoria i cui valori separano quasi perfettamente gli oggetti con l'inizio dagli altri."""
    with_start = sum(1 for v in has_start.values() if v)
    if not with_start:
        return None
    by_attr: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for oid, _ in seqs:
        for name, v in model.object_attrs.get(oid, {}).items():
            cell = by_attr[name][v]
            cell[0] += 1
            cell[1] += 1 if has_start.get(oid) else 0
    best = None
    for name, vals in by_attr.items():
        if not 2 <= len(vals) <= MAX_FILTER_VALUES:
            continue
        keep = [v for v, (tot, st) in vals.items() if st / tot >= 0.9]
        drop = [v for v in vals if v not in keep]
        covered = sum(vals[v][1] for v in keep)
        leak = sum(vals[v][1] for v in drop)
        if keep and drop and covered / with_start >= 0.9 and leak / with_start <= 0.1:
            score = (covered, -len(keep))
            if best is None or score > best[0]:
                best = (score, {"filter_attribute": name, "filter_values": sorted(keep)})
    return best[1] if best else None


def rendered(lang: str, res: dict) -> dict:
    """Il risultato del controllo con i testi gia' nella lingua dell'utente (per il JSON)."""
    out = dict(res)
    out["issues"] = [{**i, "text": render(lang, i["text"])} for i in res["issues"]]
    return out
