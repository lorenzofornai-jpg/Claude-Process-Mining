"""Control Tower: la vista di management sulle domande di business dell'assessment.

Claude traduce una volta per dataset le domande di business (e obiettivi e KPI gia' in uso) in definizioni di KPI
calcolabili sul dataset OCEL; il Data Analyst le rivede, le modifica e le approva; l'app calcola i valori in modo
deterministico a ogni apertura (nessuna chiamata a Claude per vedere i numeri).

Una definizione e' un JSON con un vocabolario chiuso, valido per qualunque processo e sistema sorgente:

- KPI: su un tipo di oggetto (di solito l'oggetto guida), una misura:
  share (quota degli oggetti che soddisfano la condizione), count (quanti), sum (somma di un importo),
  share_amount (quota dell'importo), duration (tempo in giorni tra due punti: mediana, media o 90° percentile).
  population: quali oggetti contano (es. solo le fatture con tipo documento RV); condition: quali «colpiscono».
- Condizioni: any / all / not; activity (l'oggetto, o il suo caso con gli oggetti collegati, ha una di queste
  attivita'); user (un evento di queste attivita' fatto da un utente non escluso, es. non SYSTEM*); attribute
  (valore di un attributo dell'oggetto); duration (tempo tra due punti oltre o entro N giorni).
- Punti nel tempo: la prima o l'ultima volta di un'attivita', oppure una data negli attributi dell'oggetto piu'
  eventuali giorni (es. data base di scadenza + giorni di pagamento = scadenza).
- Grafici (sul modello della galleria di Power BI): andamento mensile (linea, area, colonne, combinato volume +
  KPI), per gruppo di una dimensione (barre, colonne, tabella, Pareto, bolle, treemap, mappa geografica; dimensione
  = attributo dell'oggetto, attributo di un oggetto collegato come il nome del cliente, attributo degli eventi come
  l'utente, attivita'), gruppo per mese (colonne in pila, linee per gruppo, mappa di calore), composizione (ciambella,
  torta, imbuto delle attivita'), distribuzione (istogramma di tempi o importi), valore singolo (tachimetro,
  semaforo).

Il periodo (ultimi 3, 6, 12 mesi o tutto) si conta sul mese del primo evento di ogni oggetto, fino all'ultimo mese
del dataset; il confronto e' con lo stesso numero di mesi precedente.
"""
from __future__ import annotations

import copy
import fnmatch
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone

from app.i18n import msg

MEASURES = ("share", "count", "sum", "share_amount", "duration")
# tipi di grafico, per famiglia di dati
TREND_KINDS = ("trend", "area", "column_trend", "combo")                       # andamento mensile del KPI
GROUP_KINDS = ("bars", "columns", "table", "pareto", "bubble", "treemap", "map")  # KPI per gruppo di una dimensione
MATRIX_KINDS = ("stacked_columns", "multi_line", "heatmap")                    # gruppo per mese
COMPOSITION_KINDS = ("donut", "pie")                                           # composizione dei colpiti
SINGLE_KINDS = ("gauge", "traffic_light")                                      # valore del KPI contro l'obiettivo
CHART_KINDS = TREND_KINDS + GROUP_KINDS + MATRIX_KINDS + COMPOSITION_KINDS + ("funnel", "histogram") + SINGLE_KINDS
MAP_GROUPS = 250    # una mappa mostra tutti i paesi
SERIES = 5          # gruppi nei grafici per mese (il resto in «Altro»)
HIST_BINS = 10
STATS = ("median", "mean", "p90")
PERIODS = {"3": 3, "6": 6, "12": 12, "all": None}
TOP = 10            # gruppi nei grafici a barre e nelle tabelle
DONUT_SLICES = 6
MAX_SECTIONS = 6
MAX_KPIS = 24
DAY = 86400.0


# ---------- valori ----------

def number(value) -> float | None:
    """Importo da un attributo: 48098.24, 48.098,24, 1,234.50, 1.234.567, -12. Con entrambi i separatori il
    decimale e' l'ultimo; una sola virgola e' decimale (esportazioni europee)."""
    s = str(value if value is not None else "").strip().replace(" ", "").replace("\u00a0", "").replace("'", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", "") if s.count(",") > 1 else s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        x = float(s)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def date_value(value) -> float | None:
    """Data da un attributo (2026-05-18, 20260518, 2026-05-18T10:00:00Z, 18.05.2026), in secondi UTC."""
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%Y%m%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def month_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")


def _shift(month: str, n: int) -> str:
    y, m = int(month[:4]), int(month[5:7]) - 1 + n
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def _months(first: str, last: str) -> list[str]:
    out, m = [], first
    while m <= last and len(out) < 600:
        out.append(m)
        m = _shift(m, 1)
    return out


# ---------- catalogo del dataset (per Claude e per l'editor) ----------

def catalog(model, lead: str | None = None) -> dict:
    """Tipi di oggetto con attivita', attributi (numerici, date, testo, con esempi) e attributi degli eventi per
    attivita'. E' tutto cio' che una definizione puo' usare."""
    types = []
    for t in model.types:
        attrs: dict[str, Counter] = defaultdict(Counter)
        n = 0
        for oid, ty in model.type_of.items():
            if ty != t:
                continue
            n += 1
            for k, v in model.object_attrs.get(oid, {}).items():
                if v not in ("", "None"):
                    attrs[k][v] += 1
        acts = sorted(((a, len(o)) for (tt, a), o in model.objects_by_type_act.items() if tt == t), key=lambda x: -x[1])
        types.append({
            "name": t, "objects": n, "hub": model.is_hub(t), "lead": t == lead,
            "activities": [{"name": a, "objects": c} for a, c in acts],
            "attributes": [_attr_info(k, c) for k, c in sorted(attrs.items())],
        })
    ev: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    acts_of_event = {}
    for oid, seq in model.sequences_by_type_index.items():
        for _, act, idx in seq:
            acts_of_event[idx] = act
    for idx, attrs in enumerate(model.event_attrs):
        act = acts_of_event.get(idx)
        if act is None:
            continue
        for k, v in attrs.items():
            ev[act][k][v] += 1
    activities = sorted({a for (_, a) in model.events_by_type_act})
    return {
        "lead": lead, "types": types, "activities": activities,
        "event_attributes": {a: [_attr_info(k, c) for k, c in sorted(d.items())] for a, d in sorted(ev.items())},
        "period": _dataset_period(model),
    }


def _attr_info(name: str, counts: Counter) -> dict:
    values = list(counts)
    total = sum(counts.values()) or 1
    nums = sum(c for v, c in counts.items() if number(v) is not None)
    dates = sum(c for v, c in counts.items() if _is_date(v))
    kind = "date" if dates / total >= 0.8 else "number" if nums / total >= 0.8 else "text"
    return {"name": name, "kind": kind, "distinct": len(values), "filled": sum(counts.values()),
            "examples": [v[:40] for v, _ in counts.most_common(8)]}


def _is_date(v) -> bool:
    s = str(v).strip()
    if date_value(s) is None:
        return False
    plain = re.fullmatch(r"-?\d+([.,]\d+)?", s) is not None
    return not plain or (len(s) == 8 and s[:2] in ("19", "20"))


def _dataset_period(model) -> dict | None:
    ts = [x for x in model.event_times if not math.isnan(x)]
    if not ts:
        return None
    return {"first": month_of(min(ts)), "last": month_of(max(ts))}


# ---------- validazione ----------

def validate(spec: dict, model) -> tuple[dict, dict[str, list[dict]]]:
    """Definizioni pulite e, per KPI, i problemi che ne impediscono il calcolo (dati mancanti)."""
    spec = copy.deepcopy(spec if isinstance(spec, dict) else {})
    kpis = [k for k in spec.get("kpis") or [] if isinstance(k, dict) and k.get("id")][:MAX_KPIS]
    seen = set()
    kpis = [k for k in kpis if not (str(k["id"]) in seen or seen.add(str(k["id"])))]
    for k in kpis:
        k["id"] = str(k["id"])
    spec["kpis"] = kpis
    ids = {k["id"] for k in kpis}
    sections = []
    for i, s in enumerate(spec.get("sections") or []):
        if not isinstance(s, dict):
            continue
        s["id"] = str(s.get("id") or f"s{i + 1}")
        s["kpis"] = [str(x) for x in s.get("kpis") or [] if str(x) in ids]
        s["charts"] = [c for c in s.get("charts") or [] if isinstance(c, dict) and c.get("kind") in CHART_KINDS
                       and str(c.get("kpi")) in ids]
        for j, c in enumerate(s["charts"]):
            c["kpi"] = str(c["kpi"])
            c["id"] = str(c.get("id") or f"{s['id']}c{j + 1}")
        sections.append(s)
    spec["sections"] = sections[:MAX_SECTIONS]
    spec["headline"] = [str(x) for x in spec.get("headline") or [] if str(x) in ids][:6]

    issues: dict[str, list[dict]] = {}
    all_acts = {a for (_, a) in model.events_by_type_act}
    ev_attrs = {k for a in model.event_attrs for k in a}
    attrs_of: dict[str, set[str]] = defaultdict(set)
    for oid, ty in model.type_of.items():
        attrs_of[ty].update(model.object_attrs.get(oid, {}))

    for k in kpis:
        out: list[dict] = []
        t = k.get("object_type")
        if t not in model.object_counts:
            out.append(msg("Il tipo di oggetto «{t}» non è nel dataset.", t=str(t or "—")))
        m = k.get("measure")
        if m not in MEASURES:
            out.append(msg("Misura non riconosciuta: «{m}».", m=str(m)))
        if m in ("sum", "share_amount"):
            a = k.get("amount")
            if not a:
                out.append(msg("Manca l'attributo con l'importo: nel dataset non c'è un importo per questo oggetto."))
            elif a not in attrs_of.get(t, set()):
                out.append(msg("L'attributo «{a}» non c'è negli oggetti di tipo {t}.", a=a, t=str(t)))
            elif not _numeric(model, t, a):
                out.append(msg("L'attributo «{a}» non contiene importi numerici.", a=a))
        if m == "duration":
            d = k.get("duration") or {}
            if not d.get("from") or not d.get("to"):
                out.append(msg("Per un tempo servono un punto di inizio e un punto di fine."))
            for p in (d.get("from"), d.get("to")):
                _check_point(p, t, all_acts, attrs_of, out)
        for c in (k.get("population"), k.get("condition")):
            _check_cond(c, t, all_acts, ev_attrs, attrs_of, out)
        # problemi senza doppioni (lo stesso nome sbagliato in piu' punti)
        uniq = []
        for x in out:
            if x not in uniq:
                uniq.append(x)
        if uniq:
            issues[k["id"]] = uniq
    for s in spec["sections"]:
        for c in s["charts"]:
            k = next((x for x in kpis if x["id"] == c["kpi"]), None)
            if k is None:
                continue
            probs: list[dict] = []
            _check_dim(c.get("dimension"), k.get("object_type"), all_acts, ev_attrs, attrs_of, model, probs)
            kind = c.get("kind")
            if kind in GROUP_KINDS + MATRIX_KINDS and not isinstance(c.get("dimension"), dict):
                probs.append(msg("Per questo grafico serve una dimensione (per cosa raggruppare)."))
            if kind == "funnel":
                acts = [a for a in c.get("activities") or [] if isinstance(a, str)]
                if len(acts) < 2:
                    probs.append(msg("L'imbuto ha bisogno di almeno due attività, nell'ordine del processo."))
                for a in acts:
                    if a not in all_acts:
                        probs.append(msg("L'attività «{a}» non è nel dataset.", a=a))
            if kind == "histogram" and k.get("measure") != "duration" and not k.get("amount"):
                probs.append(msg("L'istogramma mostra come si distribuiscono tempi o importi: serve un KPI di tempo o con un importo."))
            if kind == "traffic_light" and target_value(k) is None:
                probs.append(msg("Il semaforo confronta il KPI con il suo obiettivo: imposta un obiettivo."))
            for cat in c.get("categories") or []:
                if isinstance(cat, dict):
                    _check_cond(cat.get("condition"), k.get("object_type"), all_acts, ev_attrs, attrs_of, probs)
            if probs:
                c["issues"] = probs
            else:
                c.pop("issues", None)
    return spec, issues


def _numeric(model, t, a) -> bool:
    vals = [model.object_attrs.get(oid, {}).get(a) for oid, ty in model.type_of.items() if ty == t]
    vals = [v for v in vals if v not in (None, "", "None")]
    return bool(vals) and sum(1 for v in vals if number(v) is not None) >= 0.8 * len(vals)


def _check_point(p, t, all_acts, attrs_of, out):
    if not isinstance(p, dict):
        return
    if p.get("activity") and p["activity"] not in all_acts:
        out.append(msg("L'attività «{a}» non è nel dataset.", a=p["activity"]))
    for key in ("attribute", "plus_days_attribute"):
        if p.get(key) and p[key] not in attrs_of.get(t, set()):
            out.append(msg("L'attributo «{a}» non c'è negli oggetti di tipo {t}.", a=p[key], t=str(t)))


def _check_cond(c, t, all_acts, ev_attrs, attrs_of, out):
    if not isinstance(c, dict):
        return
    op = c.get("op")
    if op in ("any", "all"):
        for x in c.get("items") or []:
            _check_cond(x, t, all_acts, ev_attrs, attrs_of, out)
    elif op == "not":
        _check_cond(c.get("item"), t, all_acts, ev_attrs, attrs_of, out)
    elif op in ("activity", "user"):
        for a in c.get("activities") or []:
            if a not in all_acts:
                out.append(msg("L'attività «{a}» non è nel dataset.", a=a))
        if op == "user":
            if not c.get("attributes"):
                out.append(msg("Manca l'attributo degli eventi con l'utente."))
            for a in c.get("attributes") or []:
                if a not in ev_attrs:
                    out.append(msg("L'attributo «{a}» non c'è negli eventi.", a=a))
    elif op == "attribute":
        if c.get("attribute") not in attrs_of.get(t, set()):
            out.append(msg("L'attributo «{a}» non c'è negli oggetti di tipo {t}.", a=str(c.get("attribute")), t=str(t)))
    elif op == "duration":
        _check_point(c.get("from"), t, all_acts, attrs_of, out)
        _check_point(c.get("to"), t, all_acts, attrs_of, out)
    else:
        out.append(msg("Condizione non riconosciuta: «{m}».", m=str(op)))


def _check_dim(d, t, all_acts, ev_attrs, attrs_of, model, out):
    if not isinstance(d, dict):
        return
    src = d.get("source")
    if src == "attribute":
        if d.get("attribute") not in attrs_of.get(t, set()):
            out.append(msg("L'attributo «{a}» non c'è negli oggetti di tipo {t}.", a=str(d.get("attribute")), t=str(t)))
    elif src == "related":
        rt = d.get("object_type")
        if rt not in model.object_counts:
            out.append(msg("Il tipo di oggetto «{t}» non è nel dataset.", t=str(rt)))
        elif d.get("attribute") and d["attribute"] not in attrs_of.get(rt, set()):
            out.append(msg("L'attributo «{a}» non c'è negli oggetti di tipo {t}.", a=d["attribute"], t=rt))
    elif src == "event_attribute":
        for a in d.get("attributes") or []:
            if a not in ev_attrs:
                out.append(msg("L'attributo «{a}» non c'è negli eventi.", a=a))
        for a in d.get("activities") or []:
            if a not in all_acts:
                out.append(msg("L'attività «{a}» non è nel dataset.", a=a))
    elif src == "activity":
        for a in d.get("activities") or []:
            if a not in all_acts:
                out.append(msg("L'attività «{a}» non è nel dataset.", a=a))


# ---------- calcolo ----------

class _Ctx:
    """Cache dei calcoli su un modello: eventi del caso di ogni oggetto, righe dei KPI."""

    def __init__(self, model):
        self.m = model
        self._case: dict[str, list] = {}
        self.rows: dict[str, list] = {}
        self.case_types = {t for t in model.types if not model.is_hub(t)}
        ts = [x for x in model.event_times if not math.isnan(x)]
        self.last_month = month_of(max(ts)) if ts else None
        self.first_month = month_of(min(ts)) if ts else None

    def events(self, oid: str, related: bool) -> list[tuple[float, str, int]]:
        own = self.m.sequences_by_type_index.get(oid, [])
        if not related:
            return own
        if oid not in self._case:
            seen = {}
            for x in self.m.case_objects(oid, self.case_types):
                for e in self.m.sequences_by_type_index.get(x, ()):
                    seen[e[2]] = e
            self._case[oid] = sorted(seen.values(), key=lambda e: (math.inf if math.isnan(e[0]) else e[0], e[2]))
        return self._case[oid]

    def first_start(self, types: set[str]) -> str | None:
        key = tuple(sorted(types))
        cache = self.__dict__.setdefault("_first", {})
        if key not in cache:
            ts = [self.start(o) for o, t in self.m.type_of.items() if t in types]
            ts = [x for x in ts if x is not None]
            cache[key] = month_of(min(ts)) if ts else self.first_month
        return cache[key]

    def last_start(self, types: set[str]) -> str | None:
        """Ultimo mese in cui inizia un oggetto di questi tipi: la fine dei periodi (dopo, il dataset ha solo la coda
        degli oggetti gia' iniziati, es. gli incassi delle ultime fatture)."""
        key = tuple(sorted(types))
        cache = self.__dict__.setdefault("_last", {})
        if key not in cache:
            ts = [self.start(o) for o, t in self.m.type_of.items() if t in types]
            ts = [x for x in ts if x is not None]
            cache[key] = month_of(max(ts)) if ts else self.last_month
        return cache[key]

    def start(self, oid: str) -> float | None:
        for ts, _, _ in self.m.sequences_by_type_index.get(oid, []):
            if not math.isnan(ts):
                return ts
        return None


def context(model) -> _Ctx:
    ctx = model.__dict__.get("_ct_ctx")
    if ctx is None:
        ctx = model.__dict__["_ct_ctx"] = _Ctx(model)
    return ctx


def _excluded(value: str, patterns: list[str]) -> bool:
    v = value.strip().upper()
    return any(fnmatch.fnmatchcase(v, str(p).strip().upper()) for p in patterns if str(p).strip())


def _point(ctx: _Ctx, oid: str, p: dict) -> float | None:
    if not isinstance(p, dict):
        return None
    if p.get("activity"):
        times = [ts for ts, a, _ in ctx.events(oid, bool(p.get("related"))) if a == p["activity"] and not math.isnan(ts)]
        if not times:
            return None
        return max(times) if p.get("which") == "last" else min(times)
    if p.get("attribute"):
        attrs = ctx.m.object_attrs.get(oid, {})
        base = date_value(attrs.get(p["attribute"]))
        if base is None:
            return None
        plus = number(attrs.get(p["plus_days_attribute"])) if p.get("plus_days_attribute") else None
        plus = (plus or 0.0) + (number(p.get("plus_days")) or 0.0)
        return base + plus * DAY
    return None


def duration_days(ctx: _Ctx, oid: str, frm: dict, to: dict) -> float | None:
    a, b = _point(ctx, oid, frm), _point(ctx, oid, to)
    return None if a is None or b is None else (b - a) / DAY


def holds(ctx: _Ctx, oid: str, c) -> bool:
    """La condizione vale per l'oggetto? (None = nessuna condizione: vale sempre)"""
    if not isinstance(c, dict) or not c:
        return True
    op = c.get("op")
    if op == "any":
        return any(holds(ctx, oid, x) for x in c.get("items") or [])
    if op == "all":
        return all(holds(ctx, oid, x) for x in c.get("items") or [])
    if op == "not":
        return not holds(ctx, oid, c.get("item"))
    if op == "activity":
        acts = set(c.get("activities") or [])
        n = sum(1 for _, a, _ in ctx.events(oid, bool(c.get("related"))) if a in acts)
        return n >= max(1, int(number(c.get("min_count")) or 1))
    if op == "user":
        acts = set(c.get("activities") or [])
        attrs = c.get("attributes") or []
        exclude, include = c.get("exclude") or [], c.get("include") or []
        for _, a, idx in ctx.events(oid, bool(c.get("related"))):
            if acts and a not in acts:
                continue
            ea = ctx.m.event_attrs[idx]
            for k in attrs:
                v = ea.get(k)
                if v and not _excluded(v, exclude) and (not include or _excluded(v, include)):
                    return True
        return False
    if op == "attribute":
        v = str(ctx.m.object_attrs.get(oid, {}).get(c.get("attribute"), "") or "").strip()
        cmp = c.get("cmp") or "in"
        values = [str(x).strip() for x in (c.get("values") or [])]
        if cmp == "empty":
            return v == ""
        if cmp == "not_empty":
            return v != ""
        if cmp in ("in", "not_in"):
            hit = v in values or (number(v) is not None and any(number(x) == number(v) for x in values))
            return hit == (cmp == "in")
        if cmp in ("gt", "lt", "ge", "le"):
            a, b = number(v), number(values[0] if values else None)
            if a is None or b is None:
                return False
            return {"gt": a > b, "lt": a < b, "ge": a >= b, "le": a <= b}[cmp]
        return False
    if op == "duration":
        d = duration_days(ctx, oid, c.get("from"), c.get("to"))
        if d is None:
            return False
        lim = number(c.get("days")) or 0.0
        return {"gt": d > lim, "ge": d >= lim, "lt": d < lim, "le": d <= lim}.get(c.get("cmp") or "gt", False)
    return False


def kpi_rows(ctx: _Ctx, kpi: dict) -> list[tuple]:
    """Per ogni oggetto della popolazione: (id, mese di inizio, colpito, importo, durata in giorni)."""
    key = json.dumps({k: kpi.get(k) for k in ("object_type", "measure", "population", "condition", "amount", "duration")},
                     sort_keys=True, ensure_ascii=False)
    if key in ctx.rows:
        return ctx.rows[key]
    t, m = kpi.get("object_type"), kpi.get("measure")
    d = kpi.get("duration") or {}
    rows = []
    for oid, ty in ctx.m.type_of.items():
        if ty != t or not holds(ctx, oid, kpi.get("population")):
            continue
        start = ctx.start(oid)
        hit = holds(ctx, oid, kpi.get("condition"))
        amount = number(ctx.m.object_attrs.get(oid, {}).get(kpi["amount"])) if kpi.get("amount") else None
        dur = duration_days(ctx, oid, d.get("from"), d.get("to")) if m == "duration" and hit else None
        rows.append((oid, month_of(start) if start is not None else None, hit, amount, dur))
    if len(ctx.rows) > 200:
        ctx.rows.clear()
    ctx.rows[key] = rows
    return rows


def _stat(values: list[float], how: str) -> float | None:
    if not values:
        return None
    if how == "mean":
        return statistics.fmean(values)
    if how == "p90":
        s = sorted(values)
        return s[min(len(s) - 1, max(0, math.ceil(0.9 * len(s)) - 1))]
    return statistics.median(values)


def aggregate(kpi: dict, rows: list[tuple]) -> dict:
    m = kpi.get("measure")
    den = len(rows)
    hits = [r for r in rows if r[2]]
    amt_all = sum(r[3] for r in rows if r[3] is not None)
    amt_hit = sum(r[3] for r in hits if r[3] is not None)
    out = {"den": den, "num": len(hits), "amount": amt_hit if kpi.get("amount") else None,
           "amount_total": amt_all if kpi.get("amount") else None, "value": None}
    if m == "share":
        out["value"] = len(hits) / den if den else None
    elif m == "count":
        out["value"] = len(hits)
    elif m == "sum":
        out["value"] = amt_hit
    elif m == "share_amount":
        out["value"] = amt_hit / amt_all if amt_all else None
    elif m == "duration":
        durs = [r[4] for r in hits if r[4] is not None]
        out["value"] = _stat(durs, (kpi.get("duration") or {}).get("stat") or "median")
        out["measured"] = len(durs)
    return out


def parse_range(period: str) -> tuple[str, str] | None:
    """«2026-02..2026-05» -> ("2026-02", "2026-05"); None se non e' un intervallo di mesi."""
    import re as _re
    m = _re.fullmatch(r"(\d{4}-\d{2})\.\.(\d{4}-\d{2})", str(period or ""))
    if not m:
        return None
    a, b = m.groups()
    return (a, b) if a <= b else (b, a)


def period_months(ctx: _Ctx, period: str, last: str | None = None,
                  first: str | None = None) -> tuple[list[str] | None, list[str] | None]:
    """Mesi del periodo scelto e del periodo precedente della stessa lunghezza (None se il dataset non lo copre).
    period: un intervallo «AAAA-MM..AAAA-MM» (il cursore della pagina, dentro i mesi del dataset), gli ultimi
    3, 6, 12 mesi fino a `last`, oppure «all» (tutto: nessun confronto)."""
    last = last or ctx.last_month
    first = first or ctx.first_month
    rng = parse_range(period)
    if rng:
        a, b = rng
        if first:
            a, b = max(a, first), max(min(b, last or b), first)
        if last:
            a = min(a, last)
        cur = _months(a, b)
        n = len(cur)
        prev = [_shift(a, -i) for i in range(n, 0, -1)]
        if not first or prev[0] < first:
            prev = None   # il dataset non copre il periodo precedente: nessun confronto
        return cur, prev
    n = PERIODS.get(period)
    if n is None or last is None:
        return None, None
    cur = [_shift(last, -i) for i in range(n - 1, -1, -1)]
    prev = [_shift(cur[0], -i) for i in range(n, 0, -1)]
    if first and prev[0] < first:
        prev = None
    if first:
        cur = [m for m in cur if m >= first] or cur[-1:]   # niente mesi vuoti prima dell'inizio dei dati
    return cur, prev


def _in(rows, months):
    if months is None:
        return rows
    ms = set(months)
    return [r for r in rows if r[1] in ms]


def target_status(kpi: dict, value) -> str | None:
    t = kpi.get("target") or {}
    goal = number(t.get("value")) if isinstance(t, dict) else None
    if value is None or goal is None or t.get("op") not in (">=", "<="):
        return None
    if kpi.get("measure") in ("share", "share_amount") and goal > 1:
        goal = goal / 100.0   # obiettivo scritto in percentuale (80 = 80%)
    return "ok" if (value >= goal if t["op"] == ">=" else value <= goal) else "ko"


def target_value(kpi: dict) -> float | None:
    t = kpi.get("target") or {}
    goal = number(t.get("value")) if isinstance(t, dict) else None
    if goal is not None and kpi.get("measure") in ("share", "share_amount") and goal > 1:
        goal /= 100.0
    return goal


def better(kpi: dict) -> str | None:
    """Verso in cui il KPI migliora: dall'obiettivo, altrimenti dalla definizione."""
    t = kpi.get("target") or {}
    if isinstance(t, dict) and t.get("op") == ">=":
        return "higher"
    if isinstance(t, dict) and t.get("op") == "<=":
        return "lower"
    return kpi.get("better") if kpi.get("better") in ("higher", "lower") else None


def dimension_values(ctx: _Ctx, oid: str, d: dict) -> list[str]:
    src = d.get("source")
    if src == "attribute":
        v = str(ctx.m.object_attrs.get(oid, {}).get(d.get("attribute"), "") or "").strip()
        return [v]
    if src == "related":
        rt, attr = d.get("object_type"), d.get("attribute")
        found = set()
        for _, _, idx in ctx.m.sequences_by_type_index.get(oid, []):
            found.update(x for x in ctx.m.event_objects[idx] if ctx.m.type_of.get(x) == rt)
        if not found and not ctx.m.is_hub(rt):
            found = {x for x in ctx.m.case_objects(oid, {rt}) if ctx.m.type_of.get(x) == rt}
        vals = []
        for x in sorted(found):
            v = str(ctx.m.object_attrs.get(x, {}).get(attr, "") or "").strip() if attr else ""
            vals.append(v or x.split(":", 1)[-1])
        return sorted(set(vals)) or [""]
    if src == "event_attribute":
        acts = set(d.get("activities") or [])
        vals = set()
        for _, a, idx in ctx.events(oid, bool(d.get("related"))):
            if acts and a not in acts:
                continue
            for k in d.get("attributes") or []:
                v = ctx.m.event_attrs[idx].get(k)
                if v:
                    vals.add(v)
        return sorted(vals) or [""]
    if src == "activity":
        acts = d.get("activities") or []
        have = {a for _, a, _ in ctx.events(oid, bool(d.get("related")))}
        return [a for a in acts if a in have] or [""]
    return [""]


def _groups(ctx, kpi, rows, d):
    by: dict[str, list] = defaultdict(list)
    for r in rows:
        for v in dimension_values(ctx, r[0], d):
            by[v].append(r)
    return by


def _impact(kpi, agg):
    """Quanto pesa un gruppo: gli oggetti colpiti, o l'importo per le misure di importo."""
    if kpi.get("measure") in ("sum", "share_amount"):
        return agg.get("amount") or 0.0
    return agg["num"]


def _sorted_items(kpi: dict, items: list[dict], order: str) -> list[dict]:
    """Ordine dei gruppi: per impatto (default), per valore (crescente o decrescente) o per nome."""
    if order in ("value_desc", "value_asc"):
        items.sort(key=lambda x: (x["value"] is None, -(x["value"] or 0) if order == "value_desc" else (x["value"] or 0), x["label"]))
    elif order == "label":
        items.sort(key=lambda x: x["label"])
    else:
        # per le quote contano i gruppi con piu' oggetti colpiti (non il 100% di un gruppo da un oggetto solo)
        items.sort(key=lambda x: (-_impact(kpi, x), -(x["den"]), x["label"]))
    return items


def _group_rows(ctx, kpi, rows, d) -> dict[str, list]:
    groups = _groups(ctx, kpi, rows, d)
    if d.get("source") in ("event_attribute", "activity"):
        groups.pop("", None)   # oggetti senza nessun valore: non sono un gruppo (es. nessun intervento)
    return groups


def _nice_bins(values: list[float], n: int) -> list[dict]:
    """Classi di uguale ampiezza dal minimo al 99° percentile (i valori oltre vanno nell'ultima classe)."""
    s = sorted(values)
    lo, hi = s[0], s[min(len(s) - 1, max(0, math.ceil(0.99 * len(s)) - 1))]
    if hi <= lo:
        hi = lo + 1.0
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    bins = []
    while start + step * len(bins) <= hi or not bins:
        a = start + step * len(bins)
        bins.append({"from": a, "to": a + step, "count": 0})
        if len(bins) > 60:
            break
    for v in s:
        i = min(len(bins) - 1, max(0, int((v - start) // step)))
        bins[i]["count"] += 1
    bins[-1]["open"] = s[-1] > bins[-1]["to"]
    return bins


def chart(ctx: _Ctx, kpi: dict, c: dict, months: list[str] | None) -> dict:
    rows = kpi_rows(ctx, kpi)
    kind = c["kind"]
    out = {"id": c["id"], "kind": kind, "kpi": kpi["id"], "title": c.get("title") or kpi.get("name")}
    if c.get("issues"):
        out["issues"] = c["issues"]
        return out
    if kind in TREND_KINDS:
        starts = [r[1] for r in rows if r[1]]
        span = months or (_months(min(starts), max(starts)) if starts else [])
        by = defaultdict(list)
        for r in rows:
            if r[1]:
                by[r[1]].append(r)
        out["points"] = [{"month": m, **aggregate(kpi, by.get(m, []))} for m in span]
        out["target"] = target_value(kpi)
        return out
    if kind in MATRIX_KINDS:
        d = c.get("dimension") or {}
        starts = [r[1] for r in rows if r[1]]
        span = months or (_months(min(starts), max(starts)) if starts else [])
        rows = _in(rows, months)
        groups = _group_rows(ctx, kpi, rows, d)
        ranked = _sorted_items(kpi, [{"label": g, **aggregate(kpi, v)} for g, v in groups.items()], c.get("sort") or "impact")
        n = max(2, min(12, int(number(c.get("series")) or (12 if kind == "heatmap" else SERIES))))
        keep = [x["label"] for x in ranked[:n]]
        series = []
        for g in keep:
            by = defaultdict(list)
            for r in groups[g]:
                by[r[1]].append(r)
            series.append({"label": g, "points": [{"month": m, **aggregate(kpi, by.get(m, []))} for m in span]})
        rest = [r for g, v in groups.items() if g not in keep for r in v]
        if rest and kind == "stacked_columns":
            by = defaultdict(list)
            for r in rest:
                by[r[1]].append(r)
            series.append({"label": None, "other": True,
                           "points": [{"month": m, **aggregate(kpi, by.get(m, []))} for m in span]})
        out.update(months=span, series=series, others=max(0, len(ranked) - len(keep)), dimension=d,
                   target=target_value(kpi), amount=kpi.get("measure") in ("sum", "share_amount"))
        return out
    rows = _in(rows, months)
    if kind in GROUP_KINDS:
        d = c.get("dimension") or {}
        groups = _group_rows(ctx, kpi, rows, d)
        items = _sorted_items(kpi, [{"label": k, **aggregate(kpi, v)} for k, v in groups.items()], c.get("sort") or "impact")
        if kind == "map":
            top = MAP_GROUPS
        else:
            top = max(3, min(30, int(number(c.get("top")) or (15 if kind in ("bubble", "treemap", "pareto") else TOP))))
        out["items"] = items[:top]
        out["others"] = max(0, len(items) - top)
        out["dimension"] = d
        out["target"] = target_value(kpi)
        if kind == "pareto":
            total = sum(_impact(kpi, x) for x in items) or 0.0
            run = 0.0
            for x in out["items"]:
                run += _impact(kpi, x)
                x["cumulative"] = run / total if total else None
            out["impact_amount"] = kpi.get("measure") in ("sum", "share_amount")
        if kind in ("bubble", "treemap"):
            out["impact_amount"] = kpi.get("measure") in ("sum", "share_amount") or (
                kind == "bubble" and bool(kpi.get("amount")) and c.get("size") != "objects")
        return out
    if kind in SINGLE_KINDS:
        agg = aggregate(kpi, rows)
        goal = target_value(kpi)
        out.update(value=agg["value"], num=agg["num"], den=agg["den"], amount=agg.get("amount"),
                   amount_total=agg.get("amount_total"), measured=agg.get("measured"), target=goal,
                   op=(kpi.get("target") or {}).get("op") if isinstance(kpi.get("target"), dict) else None)
        tol = number(c.get("tolerance"))
        tol = 10.0 if tol is None else max(0.0, tol)
        out["tolerance"] = tol
        out["light"] = _light(kpi, agg["value"], goal, tol)
        if kind == "gauge":
            out.update(_gauge_scale(kpi, agg["value"], goal, tol, c))
        return out
    if kind == "funnel":
        acts = [a for a in c.get("activities") or [] if isinstance(a, str)]
        related = bool(c.get("related"))
        counts = []
        for a in acts:
            n = 0
            for r in rows:
                if any(x == a for _, x, _ in ctx.events(r[0], related)):
                    n += 1
            counts.append({"label": a, "value": n})
        out["items"] = counts
        out["den"] = len(rows)
        return out
    if kind == "histogram":
        if kpi.get("measure") == "duration":
            values = [r[4] for r in rows if r[2] and r[4] is not None]
            out["unit"] = "days"
        else:
            values = [r[3] for r in rows if (r[2] or kpi.get("measure") in ("count", "share")) and r[3] is not None]
            out["unit"] = "amount"
        n = max(4, min(30, int(number(c.get("bins")) or HIST_BINS)))
        out["bins"] = _nice_bins(values, n) if values else []
        out["count"] = len(values)
        out["median"] = _stat(values, "median") if values else None
        out["p90"] = _stat(values, "p90") if values else None
        out["target"] = target_value(kpi) if kpi.get("measure") == "duration" else None
        return out
    # donut / pie: composizione degli oggetti colpiti (o dell'importo) per categoria o per dimensione
    weight_amount = kpi.get("measure") in ("sum", "share_amount")
    hits = [r for r in rows if r[2]]

    def w(r):
        return (r[3] or 0.0) if weight_amount else 1.0
    slices: Counter = Counter()
    cats = [x for x in c.get("categories") or [] if isinstance(x, dict) and x.get("label")]
    if cats:
        for r in hits:   # chiave = posizione della categoria (l'etichetta puo' essere un testo in due lingue)
            label = next((i for i, x in enumerate(cats) if holds(ctx, r[0], x.get("condition"))), None)
            slices[label] += w(r)
        items = [{"label": x["label"], "value": slices.get(i, 0.0)} for i, x in enumerate(cats)]
        if slices.get(None):
            items.append({"label": None, "other": True, "value": slices[None]})
    elif c.get("dimension"):
        for r in hits:
            vals = dimension_values(ctx, r[0], c["dimension"])
            for v in vals:
                slices[v] += w(r) / len(vals)
        top = slices.most_common(DONUT_SLICES)
        items = [{"label": k, "value": v} for k, v in top]
        rest = sum(slices.values()) - sum(v for _, v in top)
        if rest > 0:
            items.append({"label": None, "other": True, "value": rest})
    else:
        # colpiti contro gli altri (es. fatture con intervento manuale / senza)
        items = [{"label": kpi.get("name"), "value": sum(w(r) for r in hits), "hit": True},
                 {"label": None, "rest": True, "value": sum(w(r) for r in rows if not r[2])}]
    out["items"] = [x for x in items if x["value"]]
    out["amount"] = weight_amount
    return out


def _light(kpi: dict, value, goal, tol: float) -> str | None:
    """Semaforo: verde se rispetta l'obiettivo, giallo se lo manca di poco (entro la tolleranza in % dell'obiettivo,
    o in punti percentuali per le quote), rosso altrimenti."""
    t = kpi.get("target") if isinstance(kpi.get("target"), dict) else {}
    op = t.get("op")
    if value is None or goal is None or op not in (">=", "<="):
        return None
    if (value >= goal) if op == ">=" else (value <= goal):
        return "green"
    margin = tol / 100.0 if kpi.get("measure") in ("share", "share_amount") else abs(goal) * tol / 100.0
    gap = goal - value if op == ">=" else value - goal
    return "amber" if gap <= margin else "red"


def _nice_max(x: float) -> float:
    if x <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(x))
    return next(m * mag for m in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10) if m * mag >= x)


def _gauge_scale(kpi: dict, value, goal, tol: float, c: dict) -> dict:
    """Scala del tachimetro e fasce rosso / giallo / verde attorno all'obiettivo."""
    pct = kpi.get("measure") in ("share", "share_amount")
    lo = number(c.get("min"))
    hi = number(c.get("max"))
    if pct:
        lo = 0.0 if lo is None else (lo / 100.0 if lo > 1 else lo)
        hi = 1.0 if hi is None else (hi / 100.0 if hi > 1 else hi)
    else:
        lo = 0.0 if lo is None else lo
        if hi is None:
            hi = _nice_max(max([x for x in (value, goal) if x is not None] or [1.0]) * 1.25)
    if hi <= lo:
        hi = lo + 1.0
    bands = []
    t = kpi.get("target") if isinstance(kpi.get("target"), dict) else {}
    if goal is not None and t.get("op") in (">=", "<="):
        margin = tol / 100.0 if pct else abs(goal) * tol / 100.0
        g = min(max(goal, lo), hi)
        if t["op"] == ">=":
            a = min(max(goal - margin, lo), hi)
            bands = [{"from": lo, "to": a, "light": "red"}, {"from": a, "to": g, "light": "amber"},
                     {"from": g, "to": hi, "light": "green"}]
        else:
            a = min(max(goal + margin, lo), hi)
            bands = [{"from": lo, "to": g, "light": "green"}, {"from": g, "to": a, "light": "amber"},
                     {"from": a, "to": hi, "light": "red"}]
        bands = [x for x in bands if x["to"] > x["from"]]
    return {"min": lo, "max": hi, "bands": bands}


def currency(ctx: _Ctx, kpi: dict) -> str | None:
    a = kpi.get("currency")
    if not a:
        return None
    c = Counter(str(ctx.m.object_attrs.get(oid, {}).get(a, "")).strip() for oid, ty in ctx.m.type_of.items()
                if ty == kpi.get("object_type"))
    c.pop("", None)
    return c.most_common(1)[0][0] if c else None


def compute(spec: dict, model, period: str = "12") -> dict:
    """Valori dei KPI e dati dei grafici per il periodo scelto."""
    spec, issues = validate(spec, model)
    ctx = context(model)
    period = period if period in PERIODS or parse_range(period) else "all"
    types = {k.get("object_type") for k in spec["kpis"] if k.get("object_type")}
    last, first = ctx.last_start(types), ctx.first_start(types)
    cur, prev = period_months(ctx, period, last, first)
    kpis = {}
    for k in spec["kpis"]:
        if k["id"] in issues:
            kpis[k["id"]] = {"id": k["id"], "issues": issues[k["id"]]}
            continue
        rows = kpi_rows(ctx, k)
        now = aggregate(k, _in(rows, cur))
        before = aggregate(k, _in(rows, prev)) if prev else None
        kpis[k["id"]] = {"id": k["id"], **now, "previous": before["value"] if before else None,
                         "status": target_status(k, now["value"]), "target": target_value(k), "better": better(k),
                         "currency": currency(ctx, k)}
    sections = []
    for s in spec["sections"]:
        charts = []
        for c in s["charts"]:
            k = next(x for x in spec["kpis"] if x["id"] == c["kpi"])
            if k["id"] in issues:
                charts.append({"id": c["id"], "kind": c["kind"], "kpi": k["id"], "title": c.get("title") or k.get("name"),
                               "issues": issues[k["id"]]})
            else:
                charts.append(chart(ctx, k, c, cur))
        sections.append({"id": s["id"], "charts": charts})
    # first_month / last_month: primo e ultimo mese in cui iniziano gli oggetti dei KPI (estremi del cursore)
    return {"period": period, "months": cur, "previous_months": prev, "last_month": last,
            "first_month": first, "kpis": kpis, "sections": sections}


# ---------- proposta con Claude ----------

MAX_OUTPUT_TOKENS = 16000   # anche il ragionamento del modello consuma token di output
TYPICAL_OUTPUT_TOKENS = 7000   # i testi sono nelle due lingue

GRAMMAR = """\
- kpis: id breve (k1, k2...), name (breve, da cruscotto), description (come si calcola, in parole semplici),
  object_type (di solito l'oggetto guida), measure, e i campi della misura:
  * share: quota degli oggetti della population che soddisfano condition (es. % fatture con intervento manuale).
  * count: numero di oggetti della population che soddisfano condition (condition facoltativa).
  * sum: somma dell'attributo numerico amount sugli oggetti che soddisfano condition (es. importo gestito a mano).
  * share_amount: quota dell'importo (amount) degli oggetti che soddisfano condition sul totale della population.
  * duration: tempo in giorni da duration.from a duration.to, statistica duration.stat (median | mean | p90), sugli
    oggetti che soddisfano condition (es. ritardo di incasso rispetto alla scadenza; giorni dalla fattura
    all'incasso).
  amount: attributo numerico dell'oggetto con l'importo; currency: attributo con la valuta (se c'è).
  population: condizione che restringe gli oggetti contati (null = tutti gli oggetti del tipo).
  better: "higher" o "lower" (verso in cui il KPI migliora).
  target: {"op": ">=" | "<=", "value": numero, "reason": perché} — l'obiettivo. Per share e share_amount il valore
  è una percentuale da 0 a 100. Usa l'obiettivo dichiarato nell'assessment; se non c'è, proponi un riferimento
  ragionevole e dichiaralo come proposta in reason. null se non ha senso.
- Condizioni (oggetti JSON con "op"):
  * {"op":"any"|"all","items":[...]}, {"op":"not","item":{...}}
  * {"op":"activity","activities":[...],"related":bool,"min_count":n} — l'oggetto ha almeno n eventi (default 1)
    di una di queste attività; related=true conta anche gli eventi degli oggetti collegati al suo caso (es. per una
    fattura, l'incasso che la pareggia).
  * {"op":"user","activities":[...],"attributes":[...],"exclude":[...],"include":[...],"related":bool} — almeno un
    evento di queste attività (lista vuota = qualunque) ha negli attributi degli eventi "attributes" (es. utente) un
    valore non escluso; exclude/include sono modelli con * (es. "SYSTEM*", "BATCH*"). Serve a distinguere il lavoro
    manuale da quello automatico: guarda negli esempi del catalogo quali utenti sono di sistema.
  * {"op":"attribute","attribute":A,"cmp":"in"|"not_in"|"empty"|"not_empty"|"gt"|"lt"|"ge"|"le","values":[...]}
  * {"op":"duration","from":P,"to":P,"cmp":"gt"|"ge"|"lt"|"le","days":n} — tempo tra due punti.
  Punti nel tempo P: {"activity":X,"which":"first"|"last","related":bool} oppure una data negli attributi
  dell'oggetto {"attribute":A,"plus_days_attribute":B,"plus_days":n} (es. data base + giorni di pagamento =
  scadenza).
- charts: {"kind": tipo, "kpi": id, "title": titolo breve, ...}. Tipi (come nella galleria di Power BI):
  * andamento mensile del KPI (mese del primo evento di ogni oggetto, con la linea dell'obiettivo):
    "trend" (linea), "area" (area), "column_trend" (colonne verticali per mese), "combo" (colonne del volume:
    oggetti, o importo se il KPI ha un importo, più la linea del KPI; opzione "volume": "objects" | "amount").
  * il KPI per gruppo di una dimensione ("dimension" obbligatoria, vedi sotto; primi 10 gruppi per impatto, "top"
    3-30 per cambiarlo): "bars" (barre orizzontali), "columns" (colonne verticali), "table" (tabella),
    "pareto" (colonne per impatto più la linea del cumulato %: dove si concentra l'80%), "bubble" (grafico a bolle:
    una bolla per gruppo, x = oggetti del gruppo, y = valore del KPI, dimensione = importo o oggetti colpiti;
    opzione "size": "amount" | "objects"), "treemap" (rettangoli grandi quanto l'impatto, colore = valore rispetto
    all'obiettivo), "map" (mappa geografica dei paesi colorata con il valore del KPI: la dimensione deve dare il
    paese, codice ISO a 2 o 3 lettere come LAND1 di SAP o nome del paese).
  * gruppo per mese ("dimension" obbligatoria; "series" = quanti gruppi, default 5): "stacked_columns" (colonne in
    pila: oggetti colpiti o importo per gruppo e mese, il resto in «altro»), "multi_line" (una linea del KPI per
    gruppo), "heatmap" (mappa di calore: righe = gruppi, colonne = mesi, colore = valore del KPI).
  * composizione: "donut" (ciambella) o "pie" (torta) degli oggetti colpiti dal KPI (o del loro importo per
    sum/share_amount): con "categories" [{"label":..., "condition":{...}}] (vale la prima che si applica; il resto
    va in «altro»), oppure con una "dimension", oppure senza nulla (colpiti contro gli altri).
  * "funnel" (imbuto): "activities" = attività nell'ordine del processo (almeno 2), "related": bool; per ogni
    attività quanti oggetti della population ci arrivano e la % rispetto al primo passo.
  * "histogram" (istogramma): distribuzione dei giorni per un KPI di tempo, o degli importi ("amount") per gli
    altri; "bins" = numero di classi (default 10). Mostra mediana e 90° percentile.
  * valore singolo del periodo contro l'obiettivo: "gauge" (tachimetro con fasce rosso / giallo / verde attorno
    all'obiettivo; "min" e "max" facoltativi, per le quote in %), "traffic_light" (semaforo: verde se rispetta
    l'obiettivo, giallo se lo manca entro "tolerance" — default 10, in punti % per le quote, in % dell'obiettivo
    per gli altri KPI —, rosso oltre; serve un obiettivo).
  Dimensioni: {"source":"attribute","attribute":A} (attributo dell'oggetto), {"source":"related","object_type":T,
    "attribute":A} (attributo di un oggetto collegato, es. il nome o il paese del cliente), {"source":"event_attribute",
    "attributes":[...],"activities":[...],"related":bool} (es. chi ha fatto l'intervento),
    {"source":"activity","activities":[...],"related":bool} (quali di queste attività ci sono).
  Scegli il tipo che risponde meglio alla domanda: tempo -> trend/area/combo; dove si concentra -> bars, pareto,
  treemap, map (se c'è un paese); confronto tra gruppi su due misure -> bubble; composizione -> donut/pie;
  passaggi del processo -> funnel; distribuzione di tempi -> histogram; stato rispetto all'obiettivo -> gauge o
  traffic_light; gruppi nel tempo -> stacked_columns, multi_line, heatmap.
  Preferisci per ogni sezione: un andamento del KPI principale, una scomposizione per la dimensione che spiega il
  fenomeno e, se utile, un grafico di composizione o di stato.

"""

SYSTEM_PROMPT = """\
Sei un esperto di process mining e di controllo di gestione. Progetti la «Control Tower» di un processo: una vista
per il management (C-level) che risponde alle domande di business indicate nell'assessment con KPI misurati sul
dataset object-centric (OCEL 2.0) del processo. L'app calcola i valori in modo deterministico a partire dalle tue
definizioni: tu scrivi SOLO definizioni nel vocabolario qui sotto, usando esattamente i nomi di tipi di oggetto,
attività e attributi del catalogo ("catalog"). Non inventare nomi.

Struttura (strumento control_tower):
- sections: una per domanda di business (al massimo 6; unisci domande molto simili). question = la domanda,
  riformulata in modo chiaro per un dirigente; why = perché conta (valore economico, rischio) in una frase;
  kpis = id dei KPI della sezione (2-4); charts = 1-3 grafici; missing = se la domanda non si può misurare
  (del tutto o in parte) con questi dati, cosa manca e quale tabella o campo servirebbe; altrimenti "".
- headline: id dei KPI mostrati nella sintesi (primo livello della Control Tower), sulla card della loro domanda:
  1-2 per domanda, i più importanti.
""" + GRAMMAR + """
Regole:
- Leggi con attenzione il catalogo: attività con quanti oggetti, attributi con tipo (number, date, text) ed esempi,
  attributi degli eventi per attività. Un importo deve essere un attributo "number"; una data un attributo "date".
- Pensa da manager: KPI in percentuale e in valore economico (importi) quando ci sono gli importi, tempi in giorni.
  Esempio Accounts Receivable, touchless collection: % fatture con intervento manuale (blocchi, modifiche di
  condizioni, incasso registrato da un utente non di sistema), importo delle fatture gestite a mano, composizione
  per tipo di intervento, clienti con più interventi.
- Se il dataset non permette di rispondere a una domanda, dillo in missing e non forzare KPI senza senso.
- Testi (question, why, name, description, title, label, reason, missing, notes) SEMPRE in italiano e in inglese,
  come oggetto {"it": "...", "en": "..."}, chiari per un dirigente, senza gergo tecnico del sistema sorgente.
  "language" è la lingua dell'utente: ragiona e scegli i termini pensando a lui, ma scrivi entrambe le lingue.
- object_names: per ogni tipo di oggetto usato nei KPI, come chiamarlo nelle frasi, al singolare e al plurale in
  entrambe le lingue: {"Invoice": {"it": {"one": "fattura", "many": "fatture"}, "en": {"one": "invoice",
  "many": "invoices"}}} (minuscolo, nome di business).
- Rispondi SOLO chiamando una volta lo strumento control_tower con le definizioni complete, senza altro testo.
- Se ricevi "current_spec" e "instruction", modifica le definizioni attuali secondo l'istruzione e restituiscile
  complete (tieni gli id dei KPI che non cambi).
"""

# testo nelle due lingue
TEXT = {"type": "object", "properties": {"it": {"type": "string"}, "en": {"type": "string"}}, "required": ["it", "en"]}

TOOL = {
    "name": "control_tower",
    "description": "Definizioni della Control Tower: sezioni per domanda di business, KPI e grafici.",
    "input_schema": {
        "type": "object",
        "properties": {
            "notes": {"type": "object", "description": "Nota breve per il Data Analyst (scelte e limiti principali), "
                                                         "{\"it\": ..., \"en\": ...}."},
            "object_names": {"type": "object", "description": "Nomi dei tipi di oggetto nelle frasi, singolare e plurale "
                                                               "nelle due lingue."},
            "headline": {"type": "array", "items": {"type": "string"}},
            "sections": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "question": TEXT, "why": TEXT,
                "kpis": {"type": "array", "items": {"type": "string"}},
                "charts": {"type": "array", "items": {"type": "object"}},
                "missing": TEXT}, "required": ["question", "kpis", "charts"]}},
            "kpis": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "name": TEXT, "description": TEXT,
                "object_type": {"type": "string"}, "measure": {"type": "string", "enum": list(MEASURES)},
                "population": {"type": ["object", "null"]}, "condition": {"type": ["object", "null"]},
                "amount": {"type": ["string", "null"]}, "currency": {"type": ["string", "null"]},
                "duration": {"type": ["object", "null"]}, "better": {"type": "string", "enum": ["higher", "lower"]},
                "target": {"type": ["object", "null"]}},
                "required": ["id", "name", "object_type", "measure"]}},
        },
        "required": ["sections", "kpis", "headline"],
    },
}


def _model_name() -> str:
    from app.services import llm
    return llm.model("control_tower")


def _price() -> tuple[float, float]:
    from app.services import llm
    return llm.price(_model_name())


def _compact_catalog(cat: dict) -> dict:
    """Il catalogo per Claude: senza i tipi senza attivita' ne' attributi, esempi corti."""
    return {
        "lead_object": cat["lead"], "period": cat["period"],
        "object_types": [
            {"name": t["name"], "objects": t["objects"], **({"hub": True} if t["hub"] else {}),
             "activities": t["activities"][:40],
             "attributes": [{k: a[k] for k in ("name", "kind", "distinct", "examples")} for a in t["attributes"][:40]]}
            for t in cat["types"]],
        "event_attributes": {a: [{"name": x["name"], "distinct": x["distinct"], "examples": x["examples"][:8]}
                                 for x in v[:15]] for a, v in cat["event_attributes"].items()},
    }


def build_payload(*, language: str, process_name: str, assessment: dict, cat: dict, library: list | None = None,
                  current_spec: dict | None = None, instruction: str | None = None) -> str:
    payload = {
        "language": language,
        "process_name": process_name,
        "assessment": {k: assessment.get(k) for k in ("objectives", "key_questions", "kpis", "main_object",
                                                      "start_event", "end_event") if assessment.get(k)},
        "kpi_library_hints": library or [],
        "catalog": _compact_catalog(cat),
    }
    if current_spec is not None:
        payload["current_spec"] = current_spec
        payload["instruction"] = (instruction or "").strip()[:2000]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def estimate(payload: str) -> dict:
    from app.services.ai_mapping import _CHARS_PER_TOKEN
    price_in, price_out = _price()
    tokens_in = int(len(SYSTEM_PROMPT + json.dumps(TOOL) + payload) / _CHARS_PER_TOKEN) + 50
    typical = (tokens_in * price_in + TYPICAL_OUTPUT_TOKENS * price_out) / 1_000_000
    maximum = (tokens_in * price_in + MAX_OUTPUT_TOKENS * price_out) / 1_000_000
    return {"cost_usd": round(typical, 4), "max_usd": round(maximum, 4), "model": _model_name()}


class ProposalError(RuntimeError):
    """La proposta non e' arrivata: reason e' un messaggio da tradurre (mostrato al Data Analyst)."""

    def __init__(self, reason: dict, detail: str = ""):
        super().__init__(detail or reason.get("m", ""))
        self.reason, self.detail = reason, detail


def _spec_from_text(text: str) -> dict | None:
    """Definizioni scritte come JSON nel testo (se Claude non ha usato lo strumento)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        out = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return out if isinstance(out, dict) and out.get("kpis") else None


def propose(payload: str) -> dict:
    """Chiama Claude. Ritorna {"spec", "cost_usd"}; solleva ProposalError se la proposta non arriva.

    Lo strumento non e' imposto (tool_choice forzato non e' compatibile con il ragionamento del modello): il
    prompt chiede di chiamarlo e, se Claude scrive il JSON nel testo, lo si legge da li'. Streaming: la risposta e'
    lunga e una richiesta normale potrebbe scadere."""
    import anthropic

    from app.services import llm

    client = llm.client()
    request = dict(model=_model_name(), max_tokens=llm.max_tokens("control_tower", MAX_OUTPUT_TOKENS), system=SYSTEM_PROMPT, tools=[TOOL],
                   messages=[{"role": "user", "content": payload}])

    def call(**extra):
        with client.messages.stream(**request, **extra) as stream:
            return stream.get_final_message()
    try:
        try:
            response = call(output_config={"effort": llm.effort("control_tower")})
        except (TypeError, anthropic.BadRequestError):
            response = call()
    except anthropic.APIStatusError as exc:
        print(f"Control Tower: errore dell'API ({exc.status_code}): {exc}")
        raise ProposalError(msg("L'LLM ha risposto con un errore ({c}): {d}", c=exc.status_code, d=str(exc)[:300]), str(exc))
    except anthropic.APIConnectionError as exc:
        print(f"Control Tower: Claude non raggiungibile ({exc!r})")
        raise ProposalError(msg("L'LLM non è raggiungibile dal server: controlla la connessione e riprova."), repr(exc))
    price_in, price_out = _price()
    u = response.usage
    cost = ((u.input_tokens or 0) * price_in + (u.output_tokens or 0) * price_out) / 1_000_000
    print(f"Control Tower: stop_reason={response.stop_reason}, token in/out={u.input_tokens}/{u.output_tokens}, "
          f"costo $ {cost:.3f}")
    spec = next((b.input for b in response.content if getattr(b, "type", "") == "tool_use"), None)
    if not isinstance(spec, dict) or not spec.get("kpis"):
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        spec = _spec_from_text(text)
    if not isinstance(spec, dict):
        if response.stop_reason == "max_tokens":
            raise ProposalError(msg("La risposta dell'LLM è stata interrotta perché troppo lunga: riprova."), "max_tokens")
        raise ProposalError(msg("L'LLM non ha restituito le definizioni dei KPI: riprova."), str(response.stop_reason))
    return {"spec": normalize(spec), "cost_usd": round(cost, 4)}


def normalize(spec: dict) -> dict:
    """Forma stabile delle definizioni (id dei KPI, sezioni con id, campi mancanti)."""
    spec = copy.deepcopy(spec)
    for i, k in enumerate(spec.get("kpis") or []):
        if isinstance(k, dict):
            k["id"] = str(k.get("id") or f"k{i + 1}")
    for i, s in enumerate(spec.get("sections") or []):
        if isinstance(s, dict):
            s.setdefault("id", f"s{i + 1}")
            s.setdefault("missing", "")
            s.setdefault("why", "")
    return spec


def library_hints(process_name: str, process_type: str) -> list[dict]:
    """KPI di riferimento della famiglia di processo (business_kpi_library), se si riconosce."""
    from app.services.business_kpi_library import LIBRARY
    text = f"{process_name} {process_type}".lower()
    keys = {"Accounts Receivable / Credit-to-Cash": ("receivable", "credit", "incass", "crediti", " ar", "o2c", "order to cash", "otc", "c2c"),
            "Accounts Payable": ("payable", "fornitor", " ap", "debiti"),
            "Purchase-to-Pay": ("p2p", "purchase", "acquist", "procure")}
    out = []
    for fam, words in keys.items():
        if fam in LIBRARY and any(w in f" {text}" for w in words):
            out += [{"family": fam, **x} for x in LIBRARY[fam]]
    return out[:20]


# ---------- modifiche proposte dall'assistente ----------

OPERATIONS = ("add_section", "update_section", "remove_section", "move_section", "add_kpi", "update_kpi", "remove_kpi",
              "move_kpi", "add_chart", "update_chart", "remove_chart", "move_chart", "set_summary", "update_layout")

# impaginazione: cosa si puo' cambiare senza toccare i numeri (valori ammessi)
LAYOUT = {
    "summary_columns": (1, 2, 3),          # card delle domande per riga nella sintesi
    "summary_trend": (True, False),        # andamento mensile sulle card della sintesi
    "kpi_columns": (0, 1, 2, 3, 4),        # KPI per riga nel dettaglio (0 = automatico)
    "chart_columns": (1, 2, 3),            # grafici per riga nel dettaglio
    "show_why": (True, False),             # «perche' conta» sotto la domanda
}
CHART_LAYOUT = {"width": ("auto", "full"), "height": ("small", "medium", "large"),
                "sort": ("impact", "value_desc", "value_asc", "label")}


TEXT_FIELDS = {"name", "description", "question", "why", "missing", "reason", "title", "label", "notes"}


def _no_texts(v):
    if isinstance(v, dict):
        return {k: _no_texts(x) for k, x in v.items() if k not in TEXT_FIELDS}
    if isinstance(v, list):
        return [_no_texts(x) for x in v]
    return v


def definitions_part(spec: dict | None) -> dict:
    """La parte che il Data Analyst approva: quali domande e quali KPI, come si calcolano, obiettivi. Non i testi
    (correggere un nome o tradurlo non cambia i numeri), non i grafici, non l'impaginazione (ordine, colonne) ne' la
    scelta dei KPI della sintesi."""
    spec = spec or {}
    kpis = sorted(_no_texts(spec.get("kpis") or []), key=lambda k: str(k.get("id")))
    return {"kpis": kpis,
            "sections": sorted(({"id": s.get("id"), "kpis": sorted(str(x) for x in s.get("kpis") or [])}
                                for s in spec.get("sections") or [] if isinstance(s, dict)), key=lambda x: str(x["id"]))}


def _new_id(prefix: str, taken: set[str]) -> str:
    i = 1
    while f"{prefix}{i}" in taken:
        i += 1
    return f"{prefix}{i}"


def _pos(p, n: int) -> int:
    """Posizione 0..n (0 = in testa); fuori dai limiti o assente: in fondo."""
    return int(p) if isinstance(p, (int, float)) and 0 <= int(p) <= n else n


def apply_operations(spec: dict, operations: list) -> tuple[dict, list[dict]]:
    """Applica le modifiche proposte (aggiungi, cambia, togli domande, KPI e grafici). Ritorna le definizioni nuove
    e le operazioni non applicate (messaggi da tradurre). Un id gia' usato riceve un id nuovo, e i riferimenti
    successivi nella stessa proposta lo seguono."""
    spec = normalize(copy.deepcopy(spec or {}))
    spec.setdefault("kpis", [])
    spec.setdefault("sections", [])
    spec.setdefault("headline", [])
    skipped: list[dict] = []
    remap: dict[str, str] = {}

    def sec(sid):
        return next((s for s in spec["sections"] if s.get("id") == remap.get(str(sid), str(sid))), None)

    def kpi(kid):
        return next((k for k in spec["kpis"] if k.get("id") == remap.get(str(kid), str(kid))), None)

    def charts():
        return [c for s in spec["sections"] for c in s.get("charts") or []]

    for op in operations or []:
        if not isinstance(op, dict) or op.get("type") not in OPERATIONS:
            skipped.append(msg("Modifica non riconosciuta: «{m}».", m=str((op or {}).get("type") if isinstance(op, dict) else op)))
            continue
        t = op["type"]
        changes = op.get("changes") if isinstance(op.get("changes"), dict) else {}
        if t == "add_section":
            s = dict(op.get("section") or {})
            want = str(s.get("id") or "")
            sid = want if want and not sec(want) else _new_id("s", {x["id"] for x in spec["sections"]})
            if want and want != sid:
                remap[want] = sid
            s.update(id=sid, kpis=[remap.get(str(x), str(x)) for x in s.get("kpis") or []],
                     charts=[c for c in s.get("charts") or [] if isinstance(c, dict)])
            s.setdefault("question", "")
            s.setdefault("why", "")
            s.setdefault("missing", "")
            spec["sections"].append(s)
            continue
        if t in ("update_section", "remove_section", "add_kpi", "add_chart", "set_summary"):
            s = sec(op.get("section_id"))
            if s is None:
                skipped.append(msg("La domanda «{s}» non c'è.", s=str(op.get("section_id"))))
                continue
        if t == "update_section":
            for k in ("question", "why", "missing"):
                if k in changes:
                    s[k] = changes[k] or ""
        elif t == "remove_section":
            spec["sections"].remove(s)
            used = {k for x in spec["sections"] for k in x.get("kpis") or []}
            spec["kpis"] = [k for k in spec["kpis"] if k["id"] in used]
        elif t == "add_kpi":
            k = dict(op.get("kpi") or {})
            want = str(k.get("id") or "")
            kid = want if want and not kpi(want) else _new_id("k", {x["id"] for x in spec["kpis"]})
            if want and want != kid:
                remap[want] = kid
            k["id"] = kid
            spec["kpis"].append(k)
            s.setdefault("kpis", []).append(kid)
            if op.get("summary"):
                spec["headline"].append(kid)
        elif t == "update_kpi":
            k = kpi(op.get("kpi_id"))
            if k is None:
                skipped.append(msg("Il KPI «{k}» non c'è.", k=str(op.get("kpi_id"))))
                continue
            for key, v in changes.items():
                if key == "id":
                    continue
                if v is None:
                    k.pop(key, None)
                else:
                    k[key] = v
        elif t == "remove_kpi":
            k = kpi(op.get("kpi_id"))
            if k is None:
                skipped.append(msg("Il KPI «{k}» non c'è.", k=str(op.get("kpi_id"))))
                continue
            spec["kpis"].remove(k)
            spec["headline"] = [x for x in spec["headline"] if x != k["id"]]
            for x in spec["sections"]:
                x["kpis"] = [i for i in x.get("kpis") or [] if i != k["id"]]
                x["charts"] = [c for c in x.get("charts") or [] if c.get("kpi") != k["id"]]
        elif t == "add_chart":
            c = dict(op.get("chart") or {})
            c["kpi"] = remap.get(str(c.get("kpi")), str(c.get("kpi")))
            want = str(c.get("id") or "")
            taken = {x.get("id") for x in charts()}
            c["id"] = want if want and want not in taken else _new_id(s["id"] + "c", taken)
            lst = s.setdefault("charts", [])
            pos = op.get("position")
            lst.insert(int(pos) if isinstance(pos, int) and 0 <= pos <= len(lst) else len(lst), c)
        elif t in ("update_chart", "remove_chart"):
            owner = next((x for x in spec["sections"] for c in x.get("charts") or [] if c.get("id") == op.get("chart_id")), None)
            if owner is None:
                skipped.append(msg("Il grafico «{c}» non c'è.", c=str(op.get("chart_id"))))
                continue
            c = next(c for c in owner["charts"] if c.get("id") == op.get("chart_id"))
            if t == "remove_chart":
                owner["charts"].remove(c)
            else:
                for key, v in changes.items():
                    if key == "id":
                        continue
                    if v is None:
                        c.pop(key, None)
                    else:
                        c[key] = remap.get(str(v), v) if key == "kpi" else v
        elif t == "move_section":
            s = sec(op.get("section_id"))
            if s is None:
                skipped.append(msg("La domanda «{s}» non c'è.", s=str(op.get("section_id"))))
                continue
            spec["sections"].remove(s)
            spec["sections"].insert(_pos(op.get("position"), len(spec["sections"])), s)
        elif t == "move_kpi":
            kid = remap.get(str(op.get("kpi_id")), str(op.get("kpi_id")))
            owner = next((x for x in spec["sections"] if kid in (x.get("kpis") or [])), None)
            if owner is None:
                skipped.append(msg("Il KPI «{k}» non c'è.", k=str(op.get("kpi_id"))))
                continue
            owner["kpis"].remove(kid)
            owner["kpis"].insert(_pos(op.get("position"), len(owner["kpis"])), kid)
        elif t == "move_chart":
            owner = next((x for x in spec["sections"] for c in x.get("charts") or [] if c.get("id") == op.get("chart_id")), None)
            if owner is None:
                skipped.append(msg("Il grafico «{c}» non c'è.", c=str(op.get("chart_id"))))
                continue
            c = next(c for c in owner["charts"] if c.get("id") == op.get("chart_id"))
            target = sec(op.get("section_id")) if op.get("section_id") else owner
            if target is None:
                skipped.append(msg("La domanda «{s}» non c'è.", s=str(op.get("section_id"))))
                continue
            owner["charts"].remove(c)
            lst = target.setdefault("charts", [])
            lst.insert(_pos(op.get("position"), len(lst)), c)
        elif t == "update_layout":
            lay = spec.setdefault("layout", {})
            for key, v in changes.items():
                if key in LAYOUT and v in LAYOUT[key]:
                    lay[key] = v
                elif key in LAYOUT and v is None:
                    lay.pop(key, None)
                else:
                    skipped.append(msg("Impaginazione non riconosciuta: «{m}».", m=f"{key}={v}"))
        elif t == "set_summary":
            ids = [remap.get(str(x), str(x)) for x in op.get("kpi_ids") or []]
            mine = set(s.get("kpis") or [])
            spec["headline"] = [x for x in spec["headline"] if x not in mine] + [x for x in ids if x in mine]
    return spec, skipped


# ---------- testi nelle due lingue ----------
LANGS = ("it", "en")


def text_slots(spec: dict):
    """(contenitore, chiave) di ogni testo delle definizioni: domande, KPI, obiettivi, grafici, categorie, nota."""
    if spec.get("notes"):
        yield spec, "notes"
    for s in spec.get("sections") or []:
        for k in ("question", "why", "missing"):
            if s.get(k):
                yield s, k
        for c in s.get("charts") or []:
            if c.get("title"):
                yield c, "title"
            for cat in c.get("categories") or []:
                if isinstance(cat, dict) and cat.get("label"):
                    yield cat, "label"
    for k in spec.get("kpis") or []:
        for f in ("name", "description"):
            if k.get(f):
                yield k, f
        if isinstance(k.get("target"), dict) and k["target"].get("reason"):
            yield k["target"], "reason"


def untranslated(spec: dict | None, base_lang: str = "it") -> dict:
    """Testi che non hanno tutte e due le lingue: {id: {"text", "from"}}; e i tipi di oggetto dei KPI senza nomi."""
    out = {}
    if not spec:
        return {"texts": out, "object_types": []}
    for i, (holder, key) in enumerate(text_slots(spec)):
        v = holder[key]
        if isinstance(v, dict):
            have = {l: v.get(l) for l in LANGS if v.get(l)}
            if len(have) < len(LANGS) and have:
                src = next(iter(have))
                out[str(i)] = {"text": have[src], "from": src}
        elif isinstance(v, str) and v.strip():
            out[str(i)] = {"text": v, "from": base_lang}
    names = spec.get("object_names") or {}
    types = sorted({k.get("object_type") for k in spec.get("kpis") or [] if k.get("object_type")}
                   - {t for t, v in names.items() if isinstance(v, dict) and all(v.get(l) for l in LANGS)})
    return {"texts": out, "object_types": types}


TRANSLATE_PROMPT = """\
Traduci i testi di una vista di process mining per il management (domande di business, nomi e descrizioni di KPI,
titoli di grafici) tra italiano e inglese. Per ogni testo hai la lingua di partenza probabile ("from"; se il testo è
chiaramente in un'altra lingua vale quella del testo): restituisci il testo in entrambe le lingue,
{"it": ..., "en": ...}, tenendo identico quello di partenza. Stile da cruscotto, chiaro per
un dirigente, termini di business corretti (es. DSO, touchless, termini di pagamento). Per i tipi di oggetto
(object_types) dai il nome di business al singolare e al plurale, minuscolo, nelle due lingue. Rispondi SOLO con
lo strumento translations."""

TRANSLATE_TOOL = {
    "name": "translations",
    "description": "Testi tradotti e nomi dei tipi di oggetto.",
    "input_schema": {"type": "object", "properties": {
        "texts": {"type": "object", "description": "{id: {\"it\": ..., \"en\": ...}}"},
        "object_names": {"type": "object", "description": "{tipo: {\"it\": {\"one\", \"many\"}, \"en\": {...}}}"}},
        "required": ["texts", "object_names"]},
}


def translate_payload(spec: dict, base_lang: str) -> str | None:
    todo = untranslated(spec, base_lang)
    if not todo["texts"] and not todo["object_types"]:
        return None
    return json.dumps(todo, ensure_ascii=False, separators=(",", ":"))


def translate_estimate(payload: str) -> dict:
    from app.services import llm
    from app.services.ai_mapping import _CHARS_PER_TOKEN
    price_in, price_out = llm.price(llm.model("assistant"))
    tokens_in = int(len(TRANSLATE_PROMPT + json.dumps(TRANSLATE_TOOL) + payload) / _CHARS_PER_TOKEN) + 50
    tokens_out = int(len(payload) / _CHARS_PER_TOKEN * 1.6) + 200
    return {"cost_usd": round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 4),
            "max_usd": round((tokens_in * price_in + (tokens_out * 2 + 1500) * price_out) / 1_000_000, 4),
            "model": llm.model("assistant")}


def translate(spec: dict, base_lang: str) -> dict:
    """Completa con Claude i testi che hanno una lingua sola. Ritorna {"spec", "cost_usd"}."""
    import anthropic
    from app.services import llm

    spec = copy.deepcopy(spec)
    payload = translate_payload(spec, base_lang)
    if payload is None:
        return {"spec": spec, "cost_usd": 0.0}
    client = llm.client()
    request = dict(model=llm.model("assistant"), max_tokens=llm.max_tokens("assistant", 8000), system=TRANSLATE_PROMPT, tools=[TRANSLATE_TOOL],
                   messages=[{"role": "user", "content": payload}])
    try:
        with client.messages.stream(**request, output_config={"effort": llm.effort("assistant")}) as stream:
            response = stream.get_final_message()
    except (TypeError, anthropic.BadRequestError):
        with client.messages.stream(**request) as stream:
            response = stream.get_final_message()
    price_in, price_out = llm.price(llm.model("assistant"))
    u = response.usage
    cost = ((u.input_tokens or 0) * price_in + (u.output_tokens or 0) * price_out) / 1_000_000
    out = next((b.input for b in response.content if getattr(b, "type", "") == "tool_use"), None)
    if not isinstance(out, dict):
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        start, end = text.find("{"), text.rfind("}")
        try:
            out = json.loads(text[start:end + 1]) if start >= 0 else None
        except ValueError:
            out = None
    if not isinstance(out, dict):
        raise ProposalError(msg("L'LLM non ha restituito le traduzioni: riprova."), str(response.stop_reason))
    return {"spec": merge_translations(spec, out, base_lang), "cost_usd": round(cost, 4)}


def merge_translations(spec: dict, out: dict, base_lang: str = "it") -> dict:
    """Mette nelle definizioni i testi tradotti (per posizione, come in untranslated) e i nomi dei tipi."""
    texts = out.get("texts") or {}
    for i, (holder, key) in enumerate(text_slots(spec)):
        t = texts.get(str(i))
        if not isinstance(t, dict):
            continue
        v = holder[key]
        cur = dict(v) if isinstance(v, dict) else {}
        for l in LANGS:
            if not cur.get(l) and t.get(l):
                cur[l] = str(t[l])
        if isinstance(v, str):
            # testo in una lingua sola salvato senza indicarla: Claude ha riconosciuto la lingua e tenuto il testo
            cur = {l: str(t.get(l) or v) for l in LANGS}
        holder[key] = cur
    names = spec.setdefault("object_names", {})
    for typ, v in (out.get("object_names") or {}).items():
        if isinstance(v, dict):
            names[typ] = {**(names.get(typ) or {}), **{l: v[l] for l in LANGS if isinstance(v.get(l), dict)}}
    return spec
