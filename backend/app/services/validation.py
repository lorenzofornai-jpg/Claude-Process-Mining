"""Validation / Data Quality Engine (Fase F del Modulo 1).

Gira sull'OCEL log gia' prodotto (piu' il log degli scarti della
trasformazione) e restituisce una lista di esiti, ciascuno con severita',
esito pass/fail e un contatore di elementi coinvolti: e' quello che nella
UI di review appare come warning/errori da guardare prima di consolidare
la Ingestion Config.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from app.i18n import concat, joined, msg
from app.services.transformation import SkipRecord

# Qualifier assegnato dal Transformation Engine al collegamento evento->oggetto
# "nativo" (evento generato dalla stessa riga/tabella che ha creato l'oggetto).
# Usarlo invece del nome dell'event type rende i check indipendenti da come
# l'AI Mapping Service (mock o LLM reale) ha chiamato l'evento di creazione:
# un mapper diverso puo' chiamarlo "Create Purchase Order", "PO Created", ecc.
HOME_QUALIFIER = "involves"


def _check_missing_timestamps(skip_log: list[SkipRecord]) -> dict:
    by_type = defaultdict(int)
    for s in skip_log:
        if s.kind == "timestamp":
            by_type[s.event_type] += 1
    total = sum(by_type.values())
    details = (joined([msg("{e}: {n} righe scartate", e=k, n=v) for k, v in by_type.items()], "; ")
               if by_type else "nessuna riga scartata")
    return {
        "check_name": "Timestamp mancante o non valido",
        "severity": "warning",
        "passed": total == 0,
        "details": details,
        "affected_count": total,
    }


def _object_events(ocel: dict) -> tuple[dict[str, str], dict[str, list[tuple[datetime, str, bool]]]]:
    """(tipo di ogni oggetto, eventi di ogni oggetto in ordine di tempo: (istante, attivita', nativo)).
    "Nativo" = l'evento nasce dalla stessa riga che definisce l'oggetto (es. la registrazione
    della fattura per la fattura): e' cio' che fa nascere l'oggetto."""
    types = {o["id"]: o["type"] for o in ocel["objects"]}
    events: dict[str, list[tuple[datetime, str, bool]]] = defaultdict(list)
    for e in ocel["events"]:
        t = datetime.strptime(e["time"], "%Y-%m-%dT%H:%M:%SZ")
        for rel in e["relationships"]:
            events[rel["objectId"]].append((t, e["type"], rel["qualifier"] == HOME_QUALIFIER))
    for evs in events.values():
        evs.sort(key=lambda x: (x[0], not x[2]))  # a pari istante prima l'evento nativo
    return types, events


def _by_type(counts: dict[str, int], first: str | None) -> str:
    order = sorted(counts, key=lambda t: (t != first, -counts[t], t))
    return "; ".join(f"{t}: {counts[t]}" for t in order)  # solo nomi e numeri: nulla da tradurre


def _check_events_before_creation(ocel: dict, types: dict, events: dict, first: str | None) -> dict:
    """Per ogni oggetto (di qualunque tipo) la nascita e' il suo primo evento nativo:
    un altro evento collegato con un istante precedente e' un'anomalia (es. fattura
    registrata prima dell'ordine) o un errore di date/collegamenti. Gli oggetti senza
    evento nativo (es. anagrafiche) non si possono verificare e sono esclusi."""
    counts: dict[str, int] = defaultdict(int)
    examples = []
    for obj_id, evs in events.items():
        born = next((t for t, _, native in evs if native), None)
        if born is None:
            continue
        for t, activity, native in evs:
            if t < born:
                counts[types.get(obj_id, "?")] += 1
                if len(examples) < 5:
                    examples.append(msg("{a} il {d} su {o} (nato il {b})", a=activity, d=f"{t:%Y-%m-%d}", o=obj_id,
                                        b=f"{born:%Y-%m-%d}"))
    total = sum(counts.values())
    return {
        "check_name": "Evento prima della nascita dell'oggetto",
        "severity": "error",
        "passed": total == 0,
        "details": (msg("Per tipo di oggetto: {c}. Esempi: {e}", c=_by_type(counts, first), e=joined(examples, "; "))
                    if total else "nessun evento precede la nascita del proprio oggetto (controllati tutti i tipi di oggetto)"),
        "affected_count": total,
    }


MIN_OBJECTS_FOR_START = 5
MIN_TYPICAL_START_SHARE = 0.5


def _check_truncated_histories(ocel: dict, types: dict, events: dict, first: str | None) -> dict:
    """Per ogni tipo di oggetto si trova l'attivita' con cui di solito inizia la sua
    storia; gli oggetti che iniziano con un'altra attivita' sono nati prima del periodo
    estratto (tempi incompleti) oppure hanno un'anomalia di sequenza. Gli oggetti con il
    proprio evento di nascita sono esclusi: per loro vale il controllo precedente.
    Tipi con pochi oggetti o senza un inizio prevalente non si giudicano."""
    first_activity: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for obj_id, evs in events.items():
        if evs and not any(native for _, _, native in evs):
            first_activity[types.get(obj_id, "?")][evs[0][1]] += 1
    counts: dict[str, int] = {}
    notes = []
    for obj_type, starts in first_activity.items():
        total = sum(starts.values())
        typical, n = max(starts.items(), key=lambda x: x[1])
        if total < MIN_OBJECTS_FOR_START or n / total < MIN_TYPICAL_START_SHARE or n == total:
            continue
        counts[obj_type] = total - n
        others = ", ".join(f"{a} ({c})" for a, c in sorted(starts.items(), key=lambda x: -x[1]) if a != typical)
        notes.append((obj_type, msg("{t}: di solito inizia con «{a}» ({n} su {tot}); {k} iniziano con {o}",
                                    t=obj_type, a=typical, n=n, tot=total, k=total - n, o=others)))
    notes.sort(key=lambda x: (x[0] != first, x[0]))
    affected = sum(counts.values())
    return {
        "check_name": "Storico che inizia a metà",
        "severity": "warning",
        "passed": affected == 0,
        "details": (concat(msg("{x}.", x=joined([n for _, n in notes], "; ")),
                           "Di solito sono oggetti nati prima del periodo estratto, con tempi totali più brevi del vero "
                           "(valuta di escluderli dall'analisi dei tempi o di estendere il periodo), oppure casi con "
                           "un'anomalia di sequenza (vedi «Evento prima della nascita dell'oggetto»).")
                    if affected else "ogni tipo di oggetto inizia con la sua attività abituale"),
        "affected_count": affected,
    }


def _check_orphan_objects(ocel: dict) -> dict:
    """Oggetti a cui non e' collegato nessun evento, per tipo. Se un intero tipo non ha
    eventi, quasi sempre gli eventi della sua tabella sono collegati a un altro tipo
    (es. la stessa partita modellata come due oggetti): e' un problema di mapping."""
    referenced = {rel["objectId"] for e in ocel["events"] for rel in e["relationships"]}
    total: dict[str, int] = defaultdict(int)
    orphans: dict[str, list[str]] = defaultdict(list)
    for o in ocel["objects"]:
        total[o["type"]] += 1
        if o["id"] not in referenced:
            orphans[o["type"]].append(o["id"])
    parts = []
    for t in sorted(orphans, key=lambda t: -len(orphans[t])):
        n, of = len(orphans[t]), total[t]
        if n == of:
            parts.append(msg("{t}: nessuno dei {n} oggetti ha eventi — controlla nella revisione a quale oggetto sono "
                             "collegati gli eventi della sua tabella (forse lo stesso documento è modellato come due "
                             "oggetti)", t=t, n=of))
        else:
            parts.append(msg("{t}: {n} su {of} (es. {ex})", t=t, n=n, of=of, ex=", ".join(orphans[t][:3])))
    affected = sum(len(v) for v in orphans.values())
    return {
        "check_name": "Oggetti senza alcun evento collegato",
        "severity": "warning",
        "passed": affected == 0,
        "details": joined(parts, "; ") if parts else "ogni oggetto ha almeno un evento",
        "affected_count": affected,
    }


def _check_event_distribution(ocel: dict) -> dict:
    by_type = defaultdict(int)
    for e in ocel["events"]:
        by_type[e["type"]] += 1
    details = "; ".join(f"{k}: {v}" for k, v in sorted(by_type.items()))
    return {
        "check_name": "Distribuzione eventi per tipo (informativo)",
        "severity": "info",
        "passed": True,
        "details": details,
        "affected_count": len(ocel["events"]),
    }


def _check_activity_columns(skip_log: list[SkipRecord], stats: dict) -> list[dict]:
    """Esiti per i tipi di evento con colonna attivita': quali attivita' ne sono
    nate, quali valori sono rimasti senza traduzione (tenuti con il codice),
    quali righe sono state escluse o non avevano il valore."""
    activities = {
        group: produced for group, produced in (stats.get("activities") or {}).items()
        if list(produced) != [group]
    }
    unmapped = stats.get("unmapped_activity_values") or {}
    excluded: dict[str, int] = defaultdict(int)
    missing: dict[str, int] = defaultdict(int)
    for s in skip_log:
        if s.kind == "excluded":
            excluded[s.event_type] += 1
        elif s.kind == "activity":
            missing[s.event_type] += 1
    if not activities and not excluded and not missing:
        return []

    results = [{
        "check_name": "Attività lette da una colonna (informativo)",
        "severity": "info",
        "passed": True,
        "details": joined([
            "; ".join(f"{group}: " + ", ".join(f"{a} ({n})" for a, n in sorted(produced.items(), key=lambda x: -x[1]))
                      for group, produced in activities.items()),
            msg("righe escluse nel mapping: {x}", x=", ".join(f"{g}: {n}" for g, n in excluded.items())) if excluded else None,
        ] if excluded else [
            "; ".join(f"{group}: " + ", ".join(f"{a} ({n})" for a, n in sorted(produced.items(), key=lambda x: -x[1]))
                      for group, produced in activities.items()),
        ], "; "),
        "affected_count": sum(sum(p.values()) for p in activities.values()),
    }]
    if unmapped:
        results.append({
            "check_name": "Valori della colonna attività senza nome",
            "severity": "warning",
            "passed": False,
            "details": msg("{x}. Restano nel dataset con il codice: dagli un nome (o escludili) nella revisione del mapping.",
                           x=joined([msg("{g}: {v}", g=group, v=joined([msg("{c} ({n} righe)", c=v, n=n)
                                                                        for v, n in values.items()]))
                                     for group, values in unmapped.items()], "; ")),
            "affected_count": sum(sum(v.values()) for v in unmapped.values()),
        })
    if missing:
        results.append({
            "check_name": "Colonna attività vuota",
            "severity": "warning",
            "passed": False,
            "details": joined([msg("{g}: {n} righe senza valore, nessun evento generato", g=g, n=n)
                               for g, n in missing.items()], "; "),
            "affected_count": sum(missing.values()),
        })
    return results


def _check_object_split(ocel: dict, skip_log: list[SkipRecord], stats: dict) -> list[dict]:
    """Esiti della divisione di un oggetto per valore (es. documenti contabili -> fatture e incassi):
    quanti oggetti per tipo, quanti esclusi (con i loro eventi), quali valori non hanno un tipo."""
    base_of = stats.get("object_subtypes") or {}
    if not base_of:
        return []
    counts: dict[str, int] = defaultdict(int)
    for o in ocel["objects"]:
        if o["type"] in base_of:
            counts[o["type"]] += 1
    excluded = stats.get("excluded_objects") or {}
    excluded_events = sum(1 for s in skip_log if s.kind == "excluded_object")
    bases = sorted(set(base_of.values()))
    parts = []
    for b in bases:
        produced = ", ".join(f"{t} ({counts[t]})" for t, base in base_of.items() if base == b and counts[t])
        parts.append(msg("{o} diviso in: {t}", o=b, t=produced or "—"))
        if excluded.get(b):
            parts.append(msg("{n} oggetti esclusi", n=excluded[b]))
    if excluded_events:
        parts.append(msg("{n} eventi esclusi insieme ai loro oggetti", n=excluded_events))
    results = [{
        "check_name": "Oggetti divisi per valore (informativo)",
        "severity": "info",
        "passed": True,
        "details": joined(parts, "; "),
        "affected_count": sum(counts.values()),
    }]
    unmapped = stats.get("unmapped_split_values") or {}
    if unmapped:
        results.append({
            "check_name": "Valori senza tipo di oggetto",
            "severity": "warning",
            "passed": False,
            "details": msg("{x}. Restano nel tipo di oggetto del mapping: indica il tipo (o escludili) nella revisione del mapping.",
                           x=joined([msg("{g}: {v}", g=group, v=joined([msg("{c} ({n} righe)", c=v, n=n)
                                                                        for v, n in values.items()]))
                                     for group, values in unmapped.items()], "; ")),
            "affected_count": sum(sum(v.values()) for v in unmapped.values()),
        })
    return results


def _main_type(ocel: dict, main_object: str | None) -> str | None:
    """Il tipo di oggetto che corrisponde all'oggetto principale dell'assessment (testo
    libero, es. "fattura cliente"), se lo si riconosce: serve solo a mostrarlo per primo."""
    if not main_object:
        return None
    words = {w for w in main_object.lower().replace("/", " ").split() if len(w) >= 4}
    best = None
    for name in {o["type"] for o in ocel["objects"]}:
        score = sum(1 for w in words if w in name.lower())
        if score and (best is None or score > best[0]):
            best = (score, name)
    return best[1] if best else None


def run_data_quality_checks(ocel: dict, skip_log: list[SkipRecord], stats: dict | None = None,
                            main_object: str | None = None, planned_events: dict[str, str] | None = None,
                            business_objects: list[dict] | None = None) -> list[dict]:
    """Controlli su tutti i tipi di oggetto, senza dipendere dal processo (P2P, O2C, AR...).
    main_object: oggetto principale dichiarato nell'assessment (facoltativo, solo per l'ordine);
    planned_events: {tipo di evento del mapping: motivo} per gli eventi nati da date previste."""
    types, events = _object_events(ocel)
    first = _main_type(ocel, main_object)
    return [
        _check_missing_timestamps(skip_log),
        *_check_planned_events(planned_events or {}, stats or {}),
        _check_events_before_creation(ocel, types, events, first),
        _check_truncated_histories(ocel, types, events, first),
        _check_orphan_objects(ocel),
        *_check_object_split(ocel, skip_log, stats or {}),
        *_check_activity_columns(skip_log, stats or {}),
        *_check_needed_links(ocel, business_objects or []),
        _check_event_distribution(ocel),
    ]


def _check_needed_links(ocel: dict, objects: list[dict], max_hops: int = 3) -> list[dict]:
    """Oggetti di business «necessari»: quanti oggetti guida li raggiungono attraverso gli eventi (anche passando
    per altri oggetti, es. posizione d'ordine -> fattura -> pagamento). Se pochi, la misura che dipende da loro
    non regge: lo si dice con i numeri."""
    lead = next((o["name"] for o in objects if o.get("include") and o.get("role") == "lead"), None)
    needed = [o["name"] for o in objects if o.get("include") and o.get("role") == "needed" and o["name"] != lead]
    type_of = {o["id"]: o["type"] for o in ocel.get("objects", [])}
    present = set(type_of.values())
    needed = [t for t in needed if t in present]
    if not lead or lead not in present or not needed:
        return []
    neighbours: dict[str, set] = defaultdict(set)
    for e in ocel.get("events", []):
        ids = [r["objectId"] for r in e.get("relationships", []) if r.get("objectId") in type_of]
        for a in ids:
            neighbours[a].update(x for x in ids if x != a)
    leads = [oid for oid, t in type_of.items() if t == lead]
    reached: dict[str, int] = defaultdict(int)
    for oid in leads:
        seen, frontier, types = {oid}, {oid}, set()
        for _ in range(max_hops):
            nxt = set()
            for x in frontier:
                for y in neighbours.get(x, ()):
                    if y not in seen and len(seen) < 5000:
                        seen.add(y)
                        nxt.add(y)
                        types.add(type_of[y])
            frontier = nxt
            if not frontier:
                break
        for t in needed:
            if t in types:
                reached[t] += 1
    parts, weak = [], 0
    for t in needed:
        n = reached[t]
        pct = round(100 * n / len(leads)) if leads else 0
        parts.append(msg("{t}: raggiungibile da {n} oggetti {l} su {m} ({p}%)", t=t, n=n, l=lead, m=len(leads), p=pct))
        if n * 2 < len(leads):
            weak += 1
    return [{
        "check_name": "Collegamento agli oggetti necessari",
        "severity": "warning",
        "passed": weak == 0,
        "details": joined(parts + ([msg(
            "meno della metà: nella revisione aggiungi il collegamento mancante (una colonna degli eventi che contiene "
            "il numero di quell'oggetto) o chiedilo all'assistente")] if weak else []), "; "),
        "affected_count": weak,
    }]


def _check_planned_events(planned: dict[str, str], stats: dict) -> list[dict]:
    if not planned:
        return []
    produced = stats.get("activities") or {}
    parts = []
    total = 0
    for group, reason in planned.items():
        n = sum((produced.get(group) or {}).values())
        total += n
        parts.append(msg("«{g}» ({n} eventi): {r}", g=group, n=n, r=reason))
    return [{
        "check_name": "Eventi da date previste o di scadenza",
        "severity": "warning",
        "passed": False,
        "details": msg("{x}. Non sono fatti avvenuti: nel processo appariranno come passi svolti e falseranno "
                       "sequenze e tempi. Meglio tenerle come attributi (rifiuta l'evento nella revisione del mapping "
                       "o trasforma la data in attributo).", x=joined(parts, "; ")),
        "affected_count": total,
    }]
