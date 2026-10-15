"""Root Cause Analysis (RCA): terzo livello della Control Tower.

Il sintomo e' un KPI di una domanda di business, sugli stessi casi del secondo livello (filtri sui casi e periodo).
Il motore e' statistico e deterministico (zero token) e vale per qualunque processo: usa solo la struttura OCEL
(oggetti, attributi, eventi, collegamenti, utenti degli eventi).

1. Sintomo per caso: quota, conteggio, importo -> caso «colpito» si'/no dalla parte sbagliata del KPI; durata ->
   giorni del caso (peggio = piu' lungo, salvo KPI che migliora crescendo).
2. Fattori candidati: contesto (attributi dell'oggetto guida, oggetti collegati e loro attributi, valori mancanti),
   comportamento (attivita', ripetizioni, modifiche), interventi manuali (attivita' di solito automatiche eseguite
   da una persona: utenti classificati dal comportamento, o dagli istanti degli eventi), tempo (giorno della
   settimana, fine trimestre, carico di lavoro). Combinazioni di due fattori di contesto.
3. Filtri anti-banalita': niente tautologie (attivita' e attributi del KPI e dei filtri), precedenza nel tempo
   (un comportamento conta solo prima che il sintomo si manifesti), supporto minimo, effetto grande, test esatti
   con correzione di Benjamini-Hochberg, stabilita' nelle due meta' del periodo, effetto a parita' di un altro
   fattore, doppioni. Ordine per impatto (casi o giorni in eccesso).
4. Segmento contro resto (con filtri): divario scomposto in effetto mix ed effetto prestazione.

Se nulla supera i filtri la RCA lo dice con i motivi: meglio nessuna causa che una causa banale.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone

from app.i18n import msg
from app.services import control_tower as ct

METHOD = 1              # versione del metodo (entra nell'impronta: un metodo nuovo ricalcola)
MIN_CASES = 20          # casi minimi in un gruppo
MIN_SHARE = 0.03        # ... e quota minima della popolazione
MIN_BAD = 15            # casi con il sintomo per analizzare un sintomo si'/no
MIN_MEASURED = 40       # casi misurati per analizzare una durata
MIN_LIFT = 1.5          # quota: almeno 1,5 volte quella degli altri casi
MIN_DIFF = 0.05         # ... e almeno 5 punti percentuali in piu'
MIN_DAYS = 2.0          # durata: almeno 2 giorni in piu' (mediana)
MIN_REL = 0.2           # ... e almeno il 20% della mediana
Q_MAX = 0.05            # Benjamini-Hochberg
STABLE_LIFT = 1.15      # in tutte e due le meta' del periodo
MAX_VALUES = 30         # valori per attributo
MAX_FINDINGS = 5
PAIR_POOL = 20          # fattori di contesto da combinare a due a due
PAIR_GAIN = 1.3         # la combinazione deve essere molto piu' forte dei due fattori da soli
DUP_JACCARD = 0.8

USER_ATTR = re.compile(r"user|usnam|uname|ernam|aenam|created_?by|changed_?by|utente|operat|resource|risorsa|agent",
                       re.I)
TECH_NAME = re.compile(r"batch|system|^sys|rfc|^wf|workflow|job|auto|edi|idoc|interface|^api|srv|svc|service|bot|"
                       r"robot|sched|cron|daemon|integrat", re.I)
CHANGE_ACT = re.compile(r"^(set|remove|change|update|modify|edit|amend|correct|cancel|reverse|delete|block|unblock|"
                        r"release|imposta|rimuovi|modifica|aggiorna|correggi|annulla|storna|elimina|blocca|sblocca)\b"
                        r"|\b(change|modific|storno|reversal|cancell|annull)", re.I)
NAME_ATTR = re.compile(r"name|nome|descr|bezeich|text|testo|title|titolo", re.I)


# ---------- sintomo ----------

def symptom(kpi: dict) -> dict:
    """Come si legge il sintomo per caso: kind binary (bad = colpito o non colpito) o duration."""
    better = ct.better(kpi)
    if kpi.get("measure") == "duration":
        return {"kind": "duration", "worse": "lower" if better == "higher" else "higher"}
    return {"kind": "binary", "bad_is_hit": better != "higher"}


def _definition_refs(kpi: dict) -> tuple[set[str], set[str]]:
    """Attivita' e attributi usati nella definizione del KPI: non possono essere cause (tautologie)."""
    acts, attrs = set(), set()

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("activities",) and isinstance(v, list):
                    acts.update(str(a) for a in v)
                elif k == "activity" and isinstance(v, str):
                    acts.add(v)
                elif k in ("attribute", "plus_days_attribute") and isinstance(v, str):
                    attrs.add(v)
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    for key in ("population", "condition", "duration"):
        walk(kpi.get(key))
    if kpi.get("amount"):
        attrs.add(kpi["amount"])
    return acts, attrs


def _filter_refs(filters: list[dict], lead: str) -> tuple[set[tuple], set[str]]:
    """Attributi (tipo, attributo) e attivita' dei filtri attivi: gia' noti all'utente, non sono cause."""
    attrs, acts = set(), set()
    for f in filters or []:
        if f.get("kind") == "dimension":
            d = f.get("dimension") or {}
            src = d.get("source")
            if src == "attribute":
                attrs.add((f.get("type"), d.get("attribute")))
            elif src == "related":
                attrs.add((d.get("object_type"), d.get("attribute") or "__id__"))
            elif src == "event_attribute":
                for a in d.get("attributes") or []:
                    attrs.add(("__event__", a))
            elif src == "activity":
                acts.update(d.get("activities") or [])
        elif f.get("kind") == "condition":
            a2, at2 = _definition_refs({"condition": f.get("condition")})
            acts |= a2
            attrs |= {(f.get("type"), a) for a in at2}
        elif f.get("kind") == "attribute" or (f.get("attribute") and f.get("kind") in ("object", "event")):
            attrs.add((f.get("type") or "__event__", f.get("attribute")))
    return attrs, acts


def _cutoffs(ctx, kpi: dict, oids: list[str], durations: dict[str, float]) -> dict[str, float | None]:
    """Istante in cui il sintomo si manifesta: un comportamento conta solo se accade prima (dopo e' una
    conseguenza). Durata: inizio + obiettivo (o la mediana); condizione con una durata: inizio + i suoi giorni."""
    frm, days = None, None
    if kpi.get("measure") == "duration":
        frm = (kpi.get("duration") or {}).get("from")
        days = ct.target_value(kpi) if ct.better(kpi) != "higher" else None
        if days is None and durations:
            days = statistics.median(durations.values())
    else:
        def find(c):
            if isinstance(c, dict):
                if c.get("op") == "duration":
                    return c
                for x in (c.get("items") or []) + ([c.get("item")] if c.get("item") else []):
                    r = find(x)
                    if r:
                        return r
            return None
        c = find(kpi.get("condition"))
        if c:
            frm, days = c.get("from"), ct.number(c.get("days")) or 0.0
    if frm is None or days is None:
        return {o: None for o in oids}
    out = {}
    for o in oids:
        p = ct._point(ctx, o, frm)
        out[o] = None if p is None else p + days * ct.DAY
    return out


# ---------- interventi manuali ----------

def manual_profile(model) -> dict:
    """Utenti tecnici o persone (dal comportamento), attivita' di solito automatiche, eventi manuali.
    {"basis": "user" | "time" | None, "user_attrs", "manual": set(idx), "auto": {attivita': quota automatica}}"""
    cached = model.__dict__.get("_rca_manual")
    if cached is not None:
        return cached
    act_of, ts_of = {}, {}
    for seq in model.sequences_by_type_index.values():
        for ts, a, idx in seq:
            act_of[idx], ts_of[idx] = a, ts
    names = Counter()
    for attrs in model.event_attrs:
        for k in attrs:
            if USER_ATTR.search(k):
                names[k] += 1
    user_attrs = [k for k, _ in names.most_common()]
    out = {"basis": None, "user_attrs": user_attrs, "manual": set(), "auto": {}}
    if user_attrs:
        user_of = {}
        for idx in act_of:
            ea = model.event_attrs[idx]
            u = next((ea[k] for k in user_attrs if ea.get(k)), None)
            if u:
                user_of[idx] = u
        if len(user_of) >= MIN_CASES:
            by_user, burst = defaultdict(list), Counter()
            for idx, u in user_of.items():
                by_user[u].append(idx)
                if not math.isnan(ts_of[idx]):
                    burst[(u, int(ts_of[idx]))] += 1
            tech = set()
            for u, evs in by_user.items():
                n = len(evs)
                timed = [i for i in evs if not math.isnan(ts_of[i])]
                b = sum(1 for i in timed if burst[(u, int(ts_of[i]))] >= 3) / (len(timed) or 1)
                off = sum(1 for i in timed if _offhours(ts_of[i])) / (len(timed) or 1)
                if TECH_NAME.search(u) or (n >= MIN_CASES and (b >= 0.6 or off >= 0.5)):
                    tech.add(u)
            out["basis"] = "user"
            out["tech_users"] = sorted(tech)[:50]
            _auto_and_manual(out, act_of, {i: user_of[i] in tech for i in user_of})
            model.__dict__["_rca_manual"] = out
            return out
    # senza utenti: eventi in blocco (stessa attivita' nello stesso istante) = esecuzione automatica
    same = Counter((act_of[i], ts_of[i]) for i in act_of if not math.isnan(ts_of[i]))
    auto_flag = {i: same[(act_of[i], ts_of[i])] >= 5 for i in act_of if not math.isnan(ts_of[i])}
    if any(auto_flag.values()):
        out["basis"] = "time"
        _auto_and_manual(out, act_of, auto_flag)
    model.__dict__["_rca_manual"] = out
    return out


def _offhours(ts: float) -> bool:
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return d.weekday() >= 5 or d.hour < 6


def _auto_and_manual(out: dict, act_of: dict, is_auto: dict[int, bool]) -> None:
    per = defaultdict(list)
    for idx, flag in is_auto.items():
        per[act_of[idx]].append((idx, flag))
    for a, evs in per.items():
        share = sum(1 for _, f in evs if f) / len(evs)
        if len(evs) >= MIN_CASES and share >= 0.7:
            out["auto"][a] = round(share, 3)
            out["manual"].update(i for i, f in evs if not f)


# ---------- fattori ----------

class _Features:
    def __init__(self):
        self.sets: dict[str, set[str]] = {}
        self.meta: dict[str, dict] = {}

    def add(self, key: str, oids, parts: list[dict], family: str, group: str):
        s = set(oids)
        if s:
            self.sets[key] = s
            self.meta[key] = {"parts": parts, "family": family, "group": group}


def _kind_of(values) -> str:
    vals = [v for v in values if v not in ("", None)]
    if not vals:
        return "empty"
    n = len(vals)
    if sum(1 for v in vals if ct._is_date(v)) / n >= 0.8:
        return "date"
    if sum(1 for v in vals if ct.number(v) is not None) / n >= 0.8:
        return "number"
    return "text"


def _value_features(F: _Features, by_oid: dict[str, set[str]], oids: list[str], base: dict, family: str, group: str):
    """Fattori da un attributo (uno o piu' valori per caso): valori frequenti, fasce di numeri, valore mancante."""
    flat = [v for s in by_oid.values() for v in s]
    kind = _kind_of(flat)
    if kind in ("empty", "date"):
        return
    n = len(oids)
    distinct = Counter(flat)
    filled = sum(1 for o in oids if by_oid.get(o))
    if kind == "number" and len(distinct) > 8:
        nums = sorted(ct.number(v) for v in flat if ct.number(v) is not None)
        cuts = sorted({nums[int(len(nums) * q)] for q in (0.25, 0.5, 0.75)})
        edges = [None] + cuts + [None]
        bins = defaultdict(set)
        for o in oids:
            for v in by_oid.get(o, ()):
                x = ct.number(v)
                if x is not None:
                    bins[sum(1 for c in cuts if x > c)].add(o)
        for i, hit in bins.items():
            F.add(f"{group}:range:{i}", hit, [{**base, "k": "range", "lo": edges[i], "hi": edges[i + 1]}], family, group)
    else:
        if len(distinct) > 50 and len(distinct) > 0.5 * max(filled, 1):
            return   # identificativo (quasi un valore per caso): non e' un fattore
        for v, c in distinct.most_common(MAX_VALUES):
            if c < MIN_CASES:
                break
            F.add(f"{group}:v:{v}", [o for o in oids if v in by_oid.get(o, ())], [{**base, "k": "value", "value": v}],
                  family, group)
    if 0.05 <= filled / (n or 1) <= 0.95:
        F.add(f"{group}:empty", [o for o in oids if not by_oid.get(o)], [{**base, "k": "empty"}], family, group)


def _display(model, oid: str) -> str:
    attrs = model.object_attrs.get(oid, {})
    for k, v in attrs.items():
        if NAME_ATTR.search(k) and v and ct.number(v) is None:
            return f"{v} ({oid.split(':', 1)[-1]})"
    return oid.split(":", 1)[-1]


def build_features(model, ctx, lead: str, oids: list[str], cutoff: dict, excl_attrs: set, excl_acts: set,
                   prof: dict) -> _Features:
    F = _Features()
    # contesto: attributi dell'oggetto guida
    vals: dict[str, dict[str, set]] = defaultdict(dict)
    for o in oids:
        for k, v in model.object_attrs.get(o, {}).items():
            if v not in ("", "None"):
                vals[k][o] = {v}
    for a, d in vals.items():
        if a in excl_attrs or (lead, a) in excl_attrs:
            continue
        _value_features(F, d, oids, {"src": "attr", "type": lead, "attr": a}, "context", f"attr:{lead}:{a}")
    # contesto: oggetti collegati (anche trasversali, es. cliente o fornitore) e loro attributi
    rel: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    case_types = {t for t in model.types if not model.is_hub(t)}
    for o in oids:
        found = set()
        for _, _, idx in model.sequences_by_type_index.get(o, ()):
            found.update(model.event_objects[idx])
        found |= model.case_objects(o, case_types)
        for x in found:
            t = model.type_of.get(x)
            if x != o and t and t != lead:
                rel[t][o].add(x)
    for t, by in rel.items():
        if (t, "__id__") not in excl_attrs:
            ids = {o: {_display(model, x) for x in xs} for o, xs in by.items()}
            counts = Counter(v for s in ids.values() for v in s)
            if counts and counts.most_common(1)[0][1] >= MIN_CASES:
                for v, c in counts.most_common(MAX_VALUES):
                    if c < MIN_CASES:
                        break
                    F.add(f"rel:{t}:id:{v}", [o for o in oids if v in ids.get(o, ())],
                          [{"src": "rel", "type": t, "k": "object", "value": v}], "context", f"rel:{t}:__id__")
        attrs: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        for o, xs in by.items():
            for x in xs:
                for k, v in model.object_attrs.get(x, {}).items():
                    if v not in ("", "None"):
                        attrs[k][o].add(v)
        for a, d in attrs.items():
            if (t, a) in excl_attrs:
                continue
            _value_features(F, d, oids, {"src": "rel", "type": t, "attr": a}, "context", f"rel:{t}:{a}")
    # comportamento e interventi manuali, solo prima che il sintomo si manifesti
    acts, reps, manual = defaultdict(set), defaultdict(set), defaultdict(set)
    for o in oids:
        cut = cutoff.get(o)
        seen = Counter()
        for ts, a, idx in ctx.events(o, True):
            if a in excl_acts or (cut is not None and (math.isnan(ts) or ts >= cut)):
                continue
            seen[a] += 1
            if idx in prof["manual"]:
                manual[a].add(o)
        for a, c in seen.items():
            acts[a].add(o)
            if c >= 2:
                reps[a].add(o)
    for a, s in acts.items():
        change = bool(CHANGE_ACT.search(a))
        F.add(f"act:{a}", s, [{"src": "act", "k": "activity", "activity": a, "change": change}], "behavior", f"act:{a}")
    for a, s in reps.items():
        F.add(f"rep:{a}", s, [{"src": "act", "k": "repeated", "activity": a}], "behavior", f"rep:{a}")
    for a, s in manual.items():
        F.add(f"man:{a}", s, [{"src": "manual", "k": "manual", "activity": a, "basis": prof["basis"],
                                "auto_share": prof["auto"].get(a)}], "manual", f"man:{a}")
    # tempo: giorno della settimana, fine trimestre, carico di lavoro all'inizio del caso
    starts = {o: ctx.start(o) for o in oids}
    by_wd, qend, weeks = defaultdict(set), set(), defaultdict(set)
    for o, ts in starts.items():
        if ts is None:
            continue
        d = datetime.fromtimestamp(ts, tz=timezone.utc)
        by_wd[d.weekday()].add(o)
        if d.month in (3, 6, 9, 12) and d.day >= 17:
            qend.add(o)
        weeks[d.isocalendar()[:2]].add(o)
    for wd, s in by_wd.items():
        F.add(f"time:wd:{wd}", s, [{"src": "time", "k": "weekday", "value": wd}], "time", "time:wd")
    F.add("time:qend", qend, [{"src": "time", "k": "quarter_end"}], "time", "time:qend")
    if len(weeks) >= 8:
        sizes = sorted(len(s) for s in weeks.values())
        top = sizes[int(len(sizes) * 2 / 3)]
        busy = set().union(*[s for s in weeks.values() if len(s) > top]) if any(len(s) > top for s in weeks.values()) else set()
        F.add("time:load", busy, [{"src": "time", "k": "workload"}], "time", "time:load")
    return F


# ---------- statistica ----------

class _Pop:
    """Popolazione analizzata: per caso il valore del sintomo (1/0 o giorni, gia' nel verso «piu' alto = peggio»)."""

    def __init__(self, y: dict[str, float], binary: bool, start: dict[str, float | None]):
        self.y, self.binary, self.start = y, binary, start
        self.ids = set(y)
        self.total = sum(y.values())

    def effect(self, g: set[str], within: set[str] | None = None) -> dict | None:
        ids = self.ids if within is None else within
        g = g & ids
        r = ids - g
        if not g or not r:
            return None
        yg, yr = [self.y[o] for o in g], [self.y[o] for o in r]
        if self.binary:
            mg, mr = sum(yg) / len(yg), sum(yr) / len(yr)
            return {"n": len(g), "m_g": mg, "m_r": mr, "diff": mg - mr,
                    "lift": (mg / mr) if mr > 0 else (math.inf if mg > 0 else 1.0), "bad": int(sum(yg)),
                    "excess": (mg - mr) * len(g)}
        mg, mr = statistics.fmean(yg), statistics.fmean(yr)
        return {"n": len(g), "m_g": mg, "m_r": mr, "med_g": statistics.median(yg), "med_r": statistics.median(yr),
                "diff": statistics.median(yg) - statistics.median(yr), "excess": (mg - mr) * len(g)}

    def pvalue(self, g: set[str]) -> float:
        from scipy import stats as sps
        g = g & self.ids
        r = self.ids - g
        if self.binary:
            a = int(sum(self.y[o] for o in g))
            c = int(sum(self.y[o] for o in r))
            return float(sps.fisher_exact([[a, len(g) - a], [c, len(r) - c]], alternative="greater")[1])
        try:
            return float(sps.mannwhitneyu([self.y[o] for o in g], [self.y[o] for o in r], alternative="greater").pvalue)
        except ValueError:
            return 1.0


def _big(pop: _Pop, e: dict, med_all: float) -> bool:
    if pop.binary:
        return e["lift"] >= MIN_LIFT and e["diff"] >= MIN_DIFF
    return e["diff"] >= max(MIN_DAYS, MIN_REL * abs(med_all)) and e["m_g"] > e["m_r"]


def _bh(ps: list[float]) -> list[float]:
    n = len(ps)
    order = sorted(range(n), key=lambda i: ps[i])
    q = [1.0] * n
    prev = 1.0
    for rank, i in reversed(list(enumerate(order, 1))):
        prev = min(prev, ps[i] * n / rank)
        q[i] = prev
    return q


def _stable(pop: _Pop, g: set[str]) -> bool:
    timed = sorted((t, o) for o, t in pop.start.items() if t is not None)
    if len(timed) < 2 * MIN_CASES:
        return False
    half = len(timed) // 2
    for part in ({o for _, o in timed[:half]}, {o for _, o in timed[half:]}):
        e = pop.effect(g, part)
        if not e or e["n"] < 5:
            return False
        if pop.binary and (e["diff"] <= 0 or e["lift"] < STABLE_LIFT):
            return False
        if not pop.binary and e["m_g"] <= e["m_r"]:
            return False
    return True


def _adjusted(pop: _Pop, g: set[str], strata: set[str]) -> float | None:
    """Effetto di g a parita' di un altro fattore (strati: dentro / fuori dall'altro fattore)."""
    tot, w = 0.0, 0
    for part in (strata & pop.ids, pop.ids - strata):
        e = pop.effect(g, part)
        if not e or e["n"] < 3 or len(part) - e["n"] < 3:
            continue
        tot += (e["m_g"] - e["m_r"]) * e["n"]
        w += e["n"]
    return tot / w if w else None


def analyze_population(pop: _Pop, F: _Features) -> tuple[list[dict], dict]:
    n = len(pop.ids)
    vals = list(pop.y.values())
    med_all = statistics.median(vals) if vals else 0.0
    tested, rejected = [], Counter()
    singles = {}
    for key, s in F.sets.items():
        s = s & pop.ids
        if len(s) < MIN_CASES or len(s) < MIN_SHARE * n or len(s) == n:
            rejected["size"] += 1
            continue
        e = pop.effect(s)
        if not e:
            continue
        singles[key] = (s, e)
        if not _big(pop, e, med_all):
            rejected["effect"] += 1
            continue
        tested.append({"key": key, "parts": F.meta[key]["parts"], "family": F.meta[key]["family"],
                       "groups": [F.meta[key]["group"]], "set": s, **e})
    # combinazioni di due fattori di contesto, solo se molto piu' forti dei due da soli
    pool = sorted((k for k in singles if F.meta[k]["family"] == "context" and len(singles[k][0]) >= 2 * MIN_CASES),
                  key=lambda k: -len(singles[k][0]))[:PAIR_POOL]
    strength = (lambda e: e["lift"]) if pop.binary else (lambda e: e["diff"])
    for i, a in enumerate(pool):
        for b in pool[i + 1:]:
            if F.meta[a]["group"] == F.meta[b]["group"]:
                continue
            s = singles[a][0] & singles[b][0]
            if len(s) < MIN_CASES or len(s) < MIN_SHARE * n:
                continue
            e = pop.effect(s)
            if not e or not _big(pop, e, med_all):
                continue
            if strength(e) < PAIR_GAIN * max(strength(singles[a][1]), strength(singles[b][1]), 1e-9):
                continue
            tested.append({"key": a + " & " + b, "parts": F.meta[a]["parts"] + F.meta[b]["parts"],
                           "family": "combination", "groups": [F.meta[a]["group"], F.meta[b]["group"]], "set": s, **e})
    candidates = len(singles)
    if not tested:
        return [], {"candidates": candidates, **rejected}
    for t in tested:
        t["p"] = pop.pvalue(t["set"])
    # la correzione conta tutti i confronti fatti, anche quelli scartati prima per effetto piccolo
    qs = _bh([t["p"] for t in tested] + [1.0] * max(0, candidates - len(tested)))
    found = []
    for t, q in zip(tested, qs):
        t["q"] = q
        if q > Q_MAX:
            rejected["significance"] += 1
            continue
        if not _stable(pop, t["set"]):
            rejected["stability"] += 1
            continue
        found.append(t)
    # a parita' di condizioni: l'effetto deve restare confrontando casi simili rispetto a un altro fattore
    found.sort(key=lambda t: -t["excess"])
    kept = []
    for t in found:
        conf = None
        for o in found:
            if o is t or set(o["groups"]) & set(t["groups"]) or o["excess"] < 0.5 * t["excess"]:
                continue
            adj = _adjusted(pop, t["set"], o["set"])
            if adj is not None and adj < 0.5 * (t["m_g"] - t["m_r"]):
                conf = o
                break
        if conf is not None:
            rejected["confounded"] += 1
            continue
        dup = next((k for k in kept if len(k["set"] & t["set"]) / len(k["set"] | t["set"]) >= DUP_JACCARD), None)
        if dup is not None:
            dup.setdefault("same_cases", []).append(t["parts"])
            rejected["duplicate"] += 1
            continue
        kept.append(t)
    # quota del sintomo spiegata: casi in eccesso sui casi con il sintomo, giorni in eccesso sui giorni totali
    total_excess = pop.total if pop.binary else sum(abs(v) for v in pop.y.values())
    out = []
    for t in kept[:MAX_FINDINGS]:
        conf = "high" if t["q"] <= 0.01 and t["n"] >= 50 else "medium"
        f = {k: t[k] for k in ("key", "parts", "family", "n", "m_g", "m_r", "diff", "excess", "p", "q") if k in t}
        f.update({"share_pop": t["n"] / n, "confidence": conf, "same_cases": t.get("same_cases", [])[:3],
                  "excess_share": (t["excess"] / total_excess) if total_excess else None})
        if pop.binary:
            f.update(lift=None if math.isinf(t["lift"]) else t["lift"], bad=t["bad"])
        else:
            f.update(med_g=t["med_g"], med_r=t["med_r"])
        out.append(_clean_floats(f))
    rejected["beyond_top"] = max(0, len(kept) - MAX_FINDINGS)
    return out, {"candidates": candidates, **rejected}


def _clean_floats(x):
    if isinstance(x, float):
        return None if math.isnan(x) or math.isinf(x) else round(x, 6)
    if isinstance(x, dict):
        return {k: _clean_floats(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_clean_floats(v) for v in x]
    return x


# ---------- segmento contro resto ----------

def segment_vs_rest(model, pop_seg: _Pop, pop_rest: _Pop, lead: str, excl_attrs: set) -> dict:
    from scipy import stats as sps
    ys, yr = list(pop_seg.y.values()), list(pop_rest.y.values())
    ms, mr = statistics.fmean(ys), statistics.fmean(yr)
    gap = ms - mr
    try:
        if pop_seg.binary:
            a, c = int(sum(ys)), int(sum(yr))
            p = float(sps.fisher_exact([[a, len(ys) - a], [c, len(yr) - c]])[1])
        else:
            p = float(sps.mannwhitneyu(ys, yr).pvalue)
    except ValueError:
        p = 1.0
    out = {"n_seg": len(ys), "n_rest": len(yr), "m_seg": ms, "m_rest": mr, "gap": gap, "p": p,
           "worse": gap > 0 and p <= Q_MAX}
    if not pop_seg.binary:
        out.update(med_seg=statistics.median(ys), med_rest=statistics.median(yr))
    if not out["worse"]:
        return _clean_floats(out)
    # effetto mix: il segmento ha piu' casi di un tipo «difficile»? (attributi dell'oggetto guida)
    best = None
    every = list(pop_seg.ids | pop_rest.ids)
    attrs = defaultdict(dict)
    for o in every:
        for k, v in model.object_attrs.get(o, {}).items():
            if v not in ("", "None"):
                attrs[k][o] = v
    for a, d in attrs.items():
        if a in excl_attrs or (lead, a) in excl_attrs or _kind_of(d.values()) in ("date", "empty"):
            continue
        kind = _kind_of(d.values())
        if kind == "number" and len(set(d.values())) > 8:
            nums = sorted(ct.number(v) for v in d.values() if ct.number(v) is not None)
            cuts = [nums[int(len(nums) * q)] for q in (0.25, 0.5, 0.75)]
            cat = {o: f"Q{1 + sum(1 for c in cuts if (ct.number(v) or 0) > c)}" for o, v in d.items()}
        else:
            top = {v for v, _ in Counter(d.values()).most_common(12)}
            if len(set(d.values())) > 50 and len(set(d.values())) > 0.5 * len(d):
                continue
            cat = {o: (v if v in top else "\x00other") for o, v in d.items()}
        res = _mix(pop_seg, pop_rest, cat)
        if res and (best is None or res["mix"] > best["mix"]):
            best = {**res, "type": lead, "attr": a, "numeric": kind == "number" and len(set(d.values())) > 8}
    # ... o piu' casi di un oggetto collegato «difficile» (es. un fornitore lento)
    rel = defaultdict(dict)
    for o in every:
        for _, _, idx in model.sequences_by_type_index.get(o, ()):
            for x in model.event_objects[idx]:
                t = model.type_of.get(x)
                if x != o and t and t != lead and o not in rel[t]:
                    rel[t][o] = _display(model, x)
    for t, cat0 in rel.items():
        if (t, "__id__") in excl_attrs:
            continue
        top = {v for v, _ in Counter(cat0.values()).most_common(12)}
        res = _mix(pop_seg, pop_rest, {o: (v if v in top else "\x00other") for o, v in cat0.items()})
        if res and (best is None or res["mix"] > best["mix"]):
            best = {**res, "type": t, "attr": None, "numeric": False}
    if best and best["mix"] >= 0.25 * gap:
        out["mix"] = {"attr": best["attr"], "type": best["type"], "mix": best["mix"], "perf": gap - best["mix"],
                      "mix_share": best["mix"] / gap, "numeric": best["numeric"], "top": best["top"]}
    else:
        out["mix"] = None
    return _clean_floats(out)


def _mix(pop_seg: _Pop, pop_rest: _Pop, cat: dict[str, str]) -> dict | None:
    def groups(pop):
        g = defaultdict(list)
        for o, y in pop.y.items():
            g[cat.get(o, "\x00empty")].append(y)
        return g
    gs, gr = groups(pop_seg), groups(pop_rest)
    ns, nr = len(pop_seg.y), len(pop_rest.y)
    mr_all = statistics.fmean(pop_rest.y.values())
    mix, contrib = 0.0, []
    for c in set(gs) | set(gr):
        ws, wr = len(gs.get(c, [])) / ns, len(gr.get(c, [])) / nr
        m_rc = statistics.fmean(gr[c]) if len(gr.get(c, [])) >= 5 else mr_all
        part = (ws - wr) * (m_rc - mr_all)
        mix += part
        contrib.append({"value": None if c.startswith("\x00") else c, "w_seg": ws, "w_rest": wr, "m_rest": m_rc,
                        "part": part})
    # le categorie piu' presenti nel segmento e piu' lente (o piu' colpite) nel resto
    contrib = [c for c in sorted(contrib, key=lambda c: -c["part"]) if c["part"] > 0 and c["w_seg"] > c["w_rest"]][:3]
    return {"mix": mix, "top": contrib} if contrib else None


# ---------- analisi completa ----------

def analyze(full, seg, kpi: dict, months: list[str] | None, filters: list[dict]) -> dict:
    """full: il dataset intero; seg: il dataset filtrato (o lo stesso). months: i mesi del periodo (None = tutti)."""
    lead = kpi.get("object_type")
    sym = symptom(kpi)
    binary = sym["kind"] == "binary"
    ctx = ct.context(seg)

    def population(c, rows):
        y, start = {}, {}
        for oid, _, hit, _, dur in ct._in(rows, months):
            if binary:
                y[oid] = 1.0 if (hit if sym["bad_is_hit"] else not hit) else 0.0
            elif dur is not None:
                y[oid] = dur if sym["worse"] == "higher" else -dur
            else:
                continue
            start[oid] = c.start(oid)
        return y, start
    y, start = population(ctx, ct.kpi_rows(ctx, kpi))
    pop = _Pop(y, binary, start)
    n, bad = len(y), int(sum(y.values())) if binary else None
    result = {"method": METHOD, "symptom": sym, "kpi": kpi.get("id"), "lead": lead,
              "population": {"n": n, "bad": bad, "rate": (bad / n) if binary and n else None,
                             "median": statistics.median([abs(v) for v in y.values()]) if not binary and y else None},
              "findings": [], "rejected": {}, "notes": [], "segment": None}
    # segmento contro resto: solo con filtri sui casi
    case_filters = [f for f in filters or [] if f.get("kind") != "start_month"]
    excl_attrs_f, excl_acts_f = _filter_refs(case_filters, lead)
    def_acts, def_attrs = _definition_refs(kpi)
    excl_attrs = {(lead, a) for a in def_attrs} | excl_attrs_f
    excl_acts = def_acts | excl_acts_f
    if case_filters and seg is not full:
        fctx = ct.context(full)
        yf, sf = population(fctx, ct.kpi_rows(fctx, kpi))
        rest = {o: v for o, v in yf.items() if o not in y}
        if len(rest) >= MIN_CASES and n >= MIN_CASES:
            result["segment"] = segment_vs_rest(full, pop, _Pop(rest, binary, {o: sf[o] for o in rest}), lead,
                                                excl_attrs)
        else:
            result["notes"].append(msg("Il confronto con il resto dei casi non si fa: fuori dai filtri ci sono "
                                       "meno di {n} casi.", n=MIN_CASES))
    # dati sufficienti?
    if binary and (bad < MIN_BAD or n - bad < MIN_BAD):
        result["status"] = "insufficient"
        result["notes"].insert(0, msg("Solo {b} casi su {n} hanno il sintomo (servono almeno {m} casi con il "
                                      "sintomo e {m} senza): con i dati disponibili non si possono determinare le "
                                      "cause.", b=bad, n=n, m=MIN_BAD) if bad < MIN_BAD else
                                  msg("Quasi tutti i casi hanno il sintomo ({b} su {n}): non ci sono abbastanza casi "
                                      "senza il sintomo per capire cosa li distingue.", b=bad, n=n))
        return _clean_floats(result)
    if not binary and n < MIN_MEASURED:
        result["status"] = "insufficient"
        result["notes"].insert(0, msg("Solo {n} casi misurati (ne servono almeno {m}): con i dati disponibili non si "
                                      "possono determinare le cause.", n=n, m=MIN_MEASURED))
        return _clean_floats(result)
    oids = sorted(y)
    durations = {o: abs(v) for o, v in y.items()} if not binary else {}
    cutoff = _cutoffs(ctx, kpi, oids, durations)
    prof = manual_profile(full)
    F = build_features(seg, ctx, lead, oids, cutoff, excl_attrs, excl_acts, prof)
    findings, rejected = analyze_population(pop, F)
    result["findings"], result["rejected"] = findings, rejected
    result["timing"] = "cutoff" if any(v is not None for v in cutoff.values()) else "none"
    if prof["basis"] is None:
        result["notes"].append(msg("Gli interventi manuali non si possono determinare: nessun attributo degli "
                                   "eventi dice chi li ha eseguiti e gli istanti degli eventi non mostrano "
                                   "esecuzioni automatiche in blocco."))
    elif prof["basis"] == "time":
        result["notes"].append(msg("Nessun attributo degli eventi dice chi li ha eseguiti: gli interventi manuali "
                                   "sono stimati dagli istanti degli eventi (eventi in blocco = esecuzione "
                                   "automatica), una stima più debole."))
    if result["timing"] == "none":
        result["notes"].append(msg("Il KPI non dice quando il sintomo si manifesta: i fattori di comportamento "
                                   "possono essere anche conseguenze, verificali."))
    result["status"] = "ok" if findings else "none"
    if not findings:
        result["notes"].insert(0, msg("Con i dati disponibili non emergono cause solide per questo sintomo: "
                                      "{c} fattori esaminati, {e} con un effetto troppo piccolo, {s} non "
                                      "significativi, {u} non stabili nel tempo, {f} spiegati da un altro fattore.",
                                      c=rejected.get("candidates", 0), e=rejected.get("effect", 0),
                                      s=rejected.get("significance", 0), u=rejected.get("stability", 0),
                                      f=rejected.get("confounded", 0)))
    return _clean_floats(result)


# ---------- impronta (niente analisi ripetute) ----------

def _norm_filters(filters: list[dict]) -> list:
    keep = ("kind", "type", "dimension", "values", "condition", "ranges", "scope", "attribute", "ids")
    out = [{k: f[k] for k in keep if k in f} for f in filters or [] if f.get("kind") != "start_month"]
    return sorted(out, key=lambda f: json.dumps(f, sort_keys=True, ensure_ascii=False))


def key_of(config_id: str, kpi: dict, months: list[str] | None, filters: list[dict]) -> str:
    """Impronta senza la versione del dataset: la stessa analisi su una versione precedente."""
    defn = {k: kpi.get(k) for k in ("object_type", "measure", "population", "condition", "amount", "duration",
                                     "target", "better")}
    raw = json.dumps({"m": METHOD, "c": config_id, "k": defn, "p": months or "all", "f": _norm_filters(filters)},
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def fingerprint(key: str, version) -> str:
    return hashlib.sha256(f"{key}|{version}".encode()).hexdigest()[:40]


def text_key(kpi: dict, finding: dict) -> str:
    """Chiave dei testi dell'LLM di una causa: stesso fattore per lo stesso KPI = stesso testo (senza numeri)."""
    defn = {k: kpi.get(k) for k in ("object_type", "measure", "condition", "duration", "better")}
    raw = json.dumps({"k": defn, "f": finding["parts"]}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


# ---------- testi dell'LLM (dopo i numeri, mai al posto loro) ----------

NARRATE_MAX_TOKENS = 6000
TOKENS_PER_FINDING = 450
_TEXT = {"type": "object", "properties": {"it": {"type": "string"}, "en": {"type": "string"}}, "required": ["it", "en"]}

NARRATE_SYSTEM = """\
Sei l'analista di una Root Cause Analysis in un'app di process mining object-centric (OCEL 2.0). L'app ha già
trovato con la statistica i fattori associati a un sintomo (un KPI fuori obiettivo): supporto minimo, effetto grande,
test esatti con correzione per i confronti multipli, stabilità nel tempo, effetto che resta a parità di un altro
fattore. Ricevi il sintomo, il processo, i filtri attivi e i fattori ("findings") con i loro numeri.

Per OGNI fattore chiama lo strumento rca_explanations con:
- verdict: "cause" se è plausibile che il fattore contribuisca al sintomo; "consequence" se è più probabile che sia
  una conseguenza del sintomo o una sua definizione mascherata (es. un sollecito per un pagamento già in ritardo,
  un'attività che si fa proprio perché c'è il problema); "uncertain" se non si può dire;
- title: il fattore come causa, breve (max 12 parole), comprensibile a un dirigente;
- explanation: perché questo fattore può causare il sintomo nel processo (2-3 frasi), unendo i dati alle tue
  conoscenze del processo; distingui ciò che è certo dai dati da ciò che è interpretazione;
- check: cosa verificare o fare per confermarlo (1-2 frasi concrete).
Regole: NESSUN numero nei testi (i numeri li mostra l'app e cambiano con i dati). Non aggiungere fattori che non ci
sono. I testi vanno sempre in italiano e in inglese: {"it": "...", "en": "..."}. Testo semplice, niente markdown.
Per i tipi di oggetto usa i nomi in "object_names" nella lingua giusta.
"""

NARRATE_TOOL = {
    "name": "rca_explanations",
    "description": "Testi delle cause della Root Cause Analysis, uno per fattore.",
    "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": {
            "id": {"type": "string"}, "verdict": {"type": "string", "enum": ["cause", "consequence", "uncertain"]},
            "title": _TEXT, "explanation": _TEXT, "check": _TEXT},
        "required": ["id", "verdict", "title", "explanation", "check"]}}}, "required": ["items"]},
}


def describe(parts: list[dict]) -> str:
    """Il fattore in parole (per l'LLM, in italiano tecnico)."""
    wd = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
    out = []
    for p in parts:
        k = p.get("k")
        where = f"{p.get('type')}.{p.get('attr')}" if p.get("attr") else str(p.get("type"))
        if k == "value":
            out.append(f"{where} = {p.get('value')}")
        elif k == "range":
            out.append(f"{where} tra {p.get('lo')} e {p.get('hi')}")
        elif k == "empty":
            out.append(f"{where} non compilato")
        elif k == "object":
            out.append(f"collegato a {p.get('type')} {p.get('value')}")
        elif k == "activity":
            out.append(f"il caso ha l'attività «{p.get('activity')}»" + (" (una modifica)" if p.get("change") else ""))
        elif k == "repeated":
            out.append(f"l'attività «{p.get('activity')}» si ripete")
        elif k == "manual":
            out.append(f"«{p.get('activity')}», di solito automatica, eseguita a mano da una persona")
        elif k == "weekday":
            out.append(f"il caso inizia di {wd[int(p.get('value') or 0) % 7]}")
        elif k == "quarter_end":
            out.append("il caso inizia nelle ultime due settimane di un trimestre")
        elif k == "workload":
            out.append("il caso inizia in una settimana di carico alto")
    return " e ".join(out)


def narrate_payload(*, process_name: str, kpi: dict, object_names: dict, filter_labels: list[str],
                    result: dict, findings: list[dict]) -> str:
    sym = result["symptom"]
    payload = {
        "process": process_name,
        "symptom": {"kpi": kpi.get("name"), "description": kpi.get("description"), "measure": kpi.get("measure"),
                    "object_type": kpi.get("object_type"), "target": kpi.get("target"),
                    "reading": ("caso con il sintomo = " + ("soddisfa la condizione del KPI" if sym.get("bad_is_hit")
                                                            else "non soddisfa la condizione del KPI"))
                    if sym["kind"] == "binary" else "giorni del caso (più giorni = peggio)"},
        "object_names": object_names,
        "case_filters": filter_labels,
        "population": result.get("population"),
        "findings": [{"id": f["text_key"], "factor": describe(f["parts"]), "family": f["family"],
                      "cases": f["n"], "symptom_in_group": f.get("m_g"), "symptom_elsewhere": f.get("m_r"),
                      "timing": "prima che il sintomo si manifesti" if result.get("timing") == "cutoff" else "non noto"}
                     for f in findings],
    }
    return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


def narrate_estimate(n_findings: int, payload_chars: int = 3000) -> dict:
    from app.services import llm
    from app.services.ai_mapping import _CHARS_PER_TOKEN
    price_in, price_out = llm.price(llm.model("assistant"))
    tokens_in = int((len(NARRATE_SYSTEM) + len(json.dumps(NARRATE_TOOL)) + payload_chars) / _CHARS_PER_TOKEN)
    typical = (tokens_in * price_in + n_findings * TOKENS_PER_FINDING * price_out) / 1_000_000
    maximum = (tokens_in * price_in + NARRATE_MAX_TOKENS * price_out) / 1_000_000
    return {"cost_usd": round(typical, 4), "max_usd": round(maximum, 4), "model": llm.model("assistant")}


def narrate(payload: str) -> dict:
    """Una chiamata all'LLM. Ritorna {"items": {id: {verdict, title, explanation, check}}, "cost_usd"}."""
    from app.services import llm
    client = llm.client()
    response = client.messages.create(model=llm.model("assistant"),
                                      max_tokens=llm.max_tokens("assistant", NARRATE_MAX_TOKENS),
                                      system=NARRATE_SYSTEM, tools=[NARRATE_TOOL],
                                      messages=[{"role": "user", "content": payload}])
    price_in, price_out = llm.price(llm.model("assistant"))
    u = response.usage
    cost = ((u.input_tokens or 0) * price_in + (u.output_tokens or 0) * price_out) / 1_000_000
    items = {}
    for b in response.content:
        if getattr(b, "type", "") == "tool_use" and isinstance(b.input, dict):
            for it in b.input.get("items") or []:
                if isinstance(it, dict) and it.get("id"):
                    items[str(it["id"])] = {
                        "verdict": it.get("verdict") if it.get("verdict") in ("cause", "consequence", "uncertain")
                        else "uncertain",
                        **{k: it.get(k) for k in ("title", "explanation", "check") if isinstance(it.get(k), dict)}}
    return {"items": items, "cost_usd": round(cost, 4)}
