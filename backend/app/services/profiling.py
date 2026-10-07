"""Profilazione deterministica dei dati caricati (nessuna chiamata AI).

Gira subito dopo l'upload, prima di controllo di pertinenza e mapping AI, e
risponde alle domande che di solito emergono solo a progetto avanzato:
- ogni tabella ha una chiave univoca? ci sono righe duplicate?
- le tabelle si collegano tra loro? quante righe restano orfane?
- le date sono utilizzabili (ora presente, valori segnaposto, futuro, batch a
  mezzanotte) e coprono il perimetro dichiarato nell'assessment?
- una tabella con una data e' in realta' un log di operazioni diverse, con una
  colonna "tipo" (azione, stato, causale, tipo movimento) che dice cosa e'
  successo in ogni riga? Vale per qualunque sistema: storico stati di un CRM,
  audit trail, movimenti con causale, export gia' in forma di event log.

Ogni problema e' espresso come issue con severita', impatto sull'analisi e
cosa fare. Una sintesi compatta (chiavi, colonne data, collegamenti) viene
poi data all'AI Mapping come evidenza misurata (vedi compact_for_mapping).
"""
from __future__ import annotations

import itertools
import re
import warnings
from datetime import datetime

import pandas as pd

from app.i18n import concat, msg, render
from app.services.timeparts import looks_like_time, parse_time_of_day

PLACEHOLDER_DATE_RE = re.compile(
    r"^(0{8}|0{4}-0{2}-0{2}|9999-?12-?31|1900-?01-?01|1899-?12-?3[01]|1970-?01-?01)(\b|T|\s|$)"
)
COVERAGE_WARN = 0.95          # sotto questa % di corrispondenze un collegamento e' segnalato
MIN_LINK_CONTAINMENT = 0.5    # per riconoscere un collegamento tra colonne con nomi diversi
MAX_KEY_PAIR_CANDIDATES = 8
SEVERITY_ORDER = {"bloccante": 0, "attenzione": 1, "info": 2}


def _issue(severity: str, table: str | None, title: str, impact: str, action: str) -> dict:
    return {"severity": severity, "table": table, "title": title, "impact": impact, "action": action}


def _parse_dates(raw: pd.Series) -> tuple[pd.Series, pd.Series, list[str]]:
    """(date parse, maschera segnaposto, valori segnaposto trovati) per una colonna di testo."""
    s = raw.dropna().astype(str).str.strip()
    s = s[s != ""]
    placeholder = s.str.match(PLACEHOLDER_DATE_RE)
    valid = s[~placeholder]
    if valid.str.fullmatch(r"\d{8}").all() and len(valid):
        parsed = pd.to_datetime(valid, format="%Y%m%d", errors="coerce")
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            parsed = pd.to_datetime(valid, errors="coerce")
    return parsed, placeholder, sorted(s[placeholder].unique().tolist())[:3]


ID_HINT_RE = re.compile(
    r"(^id$|_id$|^id_|number|numero|_no$|_nr$|^nr|code|codice|key|chiave|^num|"
    r"belnr|ebeln|ebelp|vbeln|posnr|buzei|gjahr|mblnr|zeile|bukrs|kunnr|lifnr|matnr)",
    re.I,
)
DECIMAL_RE = re.compile(r"^-?\d+[.,]\d+$")
# nomi tipici di una colonna che dice cosa e' successo nella riga
ACTIVITY_HINT_RE = re.compile(
    r"(activit|attivit|action|azione|event|step|fase|phase|status|stato|state|^type$|_type$|^tipo|_tipo|"
    r"operation|operazion|causal|movement|moviment|transaction|transazion|change|"
    r"vgabe|bewtp|bwart|blart|vorgang|tcode)",
    re.I,
)
# nomi che dicono senza dubbi «quale operazione»: vincono su stato, tipo, causale...
STRONG_ACTIVITY_RE = re.compile(
    r"(activit|attivit|event|action|azione|operation|operazion|^step|_step|task|vorgang|concept:name)", re.I
)
# motivo di un'operazione (perche', non che cosa): attributo dell'evento, non attivita'.
# «causale» resta un indizio di attivita': in molti gestionali e' il tipo di operazione.
REASON_HINT_RE = re.compile(r"(reason|motiv|grund|rstgr|abgru|^cause$|_cause$|^causa$|_causa$)", re.I)
# numero di giorni da sommare a una data base (es. giorni di pagamento)
DAYS_HINT_RE = re.compile(
    r"(days|giorni|tage|ztag|zbd\dt|^gg|_gg$|ggpag|term_?days|net_?days|payment_?days)", re.I
)
# colonne di utenti/persone: poche modalita' testuali, ma non sono attivita'
USER_HINT_RE = re.compile(
    r"(user|utente|ernam|usnam|aenam|_by$|owner|operator|operatore|agent|person|name|nome|resource|risorsa)", re.I
)
MAX_ACTIVITY_VALUES = 40


def _key_candidates(df: pd.DataFrame, exclude: set[str]) -> list[str]:
    """Colonne che possono far parte di una chiave: sempre valorizzate, non date,
    non importi/quantita' decimali; prima quelle con nome da identificativo."""
    out = []
    for c in df.columns:
        if c in exclude or not df[c].notna().all():
            continue
        sample = df[c].astype(str).head(200)
        if sample.str.match(DECIMAL_RE).mean() > 0.5:
            continue
        out.append(c)
    return sorted(out, key=lambda c: (0 if ID_HINT_RE.search(c) else 1, list(df.columns).index(c)))


def _find_key(df: pd.DataFrame, date_cols: set[str]) -> list[str] | None:
    """Chiave candidata: una colonna (o una coppia) senza vuoti e senza ripetizioni.
    Una colonna univoca "per caso" (es. un prezzo) non conta: serve un nome da
    identificativo o valori non numerici."""
    n = len(df)
    if n < 2:
        return None
    cands = _key_candidates(df, date_cols)
    for c in cands:
        if df[c].nunique() == n and (ID_HINT_RE.search(c) or not df[c].astype(str).str.fullmatch(r"-?\d+").all()):
            return [c]
    hinted = [c for c in cands if ID_HINT_RE.search(c)][:MAX_KEY_PAIR_CANDIDATES]
    for a, b in itertools.combinations(hinted, 2):
        if not df.duplicated(subset=[a, b]).any():
            return sorted([a, b], key=list(df.columns).index)
    return None


def profile_tables(
    tables_data: dict[str, list[dict]],
    date_columns: dict[str, list[str]],
    period: tuple[str | None, str | None] = (None, None),
    today: datetime | None = None,
    time_pairs: dict[str, dict[str, str]] | None = None,
    planned: dict[str, dict[str, str]] | None = None,
    due_objectives: list[str] | None = None,
) -> dict:
    """time_pairs: {tabella: {colonna data: colonna con l'ora}} (es. CPUDT -> CPUTM):
    la data si valuta con la sua ora, come verra' usata negli eventi.
    due_objectives: obiettivi dell'assessment che hanno bisogno di una scadenza (es. puntualita' dei
    pagamenti): le date previste diventano il dato che serve a quegli obiettivi, e si dice."""
    time_pairs = time_pairs or {}
    planned = planned or {}
    today = pd.Timestamp(today or datetime.now())
    p_from = pd.to_datetime(period[0], errors="coerce") if period[0] else None
    p_to = pd.to_datetime(period[1], errors="coerce") if period[1] else None
    issues: list[dict] = []
    tables: list[dict] = []
    frames = {name: pd.DataFrame(rows) for name, rows in tables_data.items()}

    any_time_info = []  # per ogni colonna data: ha l'ora?
    for name, df in frames.items():
        n = len(df)
        t = {"name": name, "rows": n, "columns": len(df.columns), "key": None, "duplicates": 0,
             "empty_columns": [], "constant_columns": [], "dates": [],
             "planned_dates": dict(planned.get(name, {}))}
        tables.append(t)
        if n == 0:
            issues.append(_issue("bloccante", name, "La tabella è vuota",
                                 "Non produrrà né oggetti né eventi: la parte di processo che rappresenta mancherà nel dataset per l'analisi.",
                                 "Verifica i filtri dell'estrazione e ricarica il file."))
            continue

        t["duplicates"] = int(df.duplicated().sum())
        if t["duplicates"]:
            issues.append(_issue("attenzione", name, msg("{n} righe duplicate identiche", n=t["duplicates"]),
                                 "Gli eventi duplicati gonfiano conteggi e frequenze e creano finti ricicli nel processo.",
                                 "Controlla se l'estrazione ha unito più volte gli stessi dati; i duplicati esatti si possono scartare."))
        t["empty_columns"] = [c for c in df.columns if df[c].isna().all()]
        t["constant_columns"] = [c for c in df.columns if c not in t["empty_columns"] and df[c].nunique() == 1]
        if t["empty_columns"]:
            issues.append(_issue("info", name, msg("Colonne sempre vuote: {c}", c=", ".join(t["empty_columns"])),
                                 "Non contengono nessun valore, quindi non possono diventare né eventi né chiavi o "
                                 "collegamenti tra tabelle: nel dataset per l'analisi resterebbero campi vuoti. "
                                 "Per questo vengono escluse dal mapping AI (meno costi, meno righe da rivedere).",
                                 "Se dovrebbero essere valorizzate (es. un campo «approvatore» sempre vuoto), "
                                 "probabilmente l'estrazione ha preso il campo sbagliato: verificalo e ricarica i dati."))
        time_cols = set(time_pairs.get(name, {}).values())
        t["key"] = _find_key(df, set(date_columns.get(name, [])) | time_cols)
        if t["key"] is None and not t["duplicates"]:
            issues.append(_issue("attenzione", name, "Nessuna chiave univoca trovata (né una colonna né una coppia)",
                                 "Senza una chiave le righe non si possono identificare come oggetti distinti, né collegare in modo affidabile ad altre tabelle.",
                                 "Indica nella descrizione della tabella quali colonne la identificano, o includi nell'estrazione il campo chiave mancante (es. numero posizione)."))

        for col in date_columns.get(name, []):
            if col not in df.columns:
                continue
            raw = df[col]
            parsed, placeholder, placeholder_values = _parse_dates(raw)
            tc = time_pairs.get(name, {}).get(col)
            if tc in df.columns and len(parsed):
                hms = df.loc[parsed.index, tc].map(parse_time_of_day)
                add = hms.map(lambda x: pd.Timedelta(hours=x[0], minutes=x[1], seconds=x[2]) if x else pd.Timedelta(0))
                at_midnight = (parsed.dt.hour == 0) & (parsed.dt.minute == 0) & (parsed.dt.second == 0)
                parsed = parsed.where(~at_midnight, parsed + add)
            nonnull = int(raw.notna().sum())
            ok = parsed.dropna()
            d = {"column": col, "time_column": tc if tc in df.columns else None, "filled_pct": round(100 * nonnull / n), "min": None, "max": None,
                 "has_time": False, "placeholders": int(placeholder.sum()), "future": 0,
                 "unparsable": int(parsed.isna().sum()), "outside_period": 0, "midnight_pct": None}
            if len(ok):
                d["min"], d["max"] = ok.min().strftime("%Y-%m-%d"), ok.max().strftime("%Y-%m-%d")
                with_time = (ok.dt.hour != 0) | (ok.dt.minute != 0) | (ok.dt.second != 0)
                d["has_time"] = bool(with_time.any())
                if d["has_time"]:
                    d["midnight_pct"] = round(100 * (1 - with_time.mean()))
                d["future"] = int((ok > today).sum())
                if p_from is not None or p_to is not None:
                    outside = pd.Series(False, index=ok.index)  # le date future sono gia' segnalate a parte
                    if p_from is not None and not pd.isna(p_from):
                        outside |= ok < p_from
                    if p_to is not None and not pd.isna(p_to):
                        outside |= ok > p_to + pd.Timedelta(days=1)
                    d["outside_period"] = int((outside & (ok <= today)).sum())
            t["dates"].append(d)
            any_time_info.append(d["has_time"])

            label = f"{col} + {d['time_column']}" if d["time_column"] else col  # la tabella e' gia' indicata accanto al titolo
            if d["placeholders"]:
                issues.append(_issue("attenzione", name, msg("{c}: {n} date segnaposto ({v})", c=label, n=d["placeholders"], v=", ".join(placeholder_values)),
                                     "Sono valori convenzionali, non date reali: se usate come istante dell'evento falsano tempi e ordinamento.",
                                     "Di solito significano «non ancora avvenuto»: verranno trattate come date mancanti, l'evento non sarà generato per quelle righe."))
            if d["unparsable"]:
                issues.append(_issue("attenzione", name, msg("{c}: {n} valori non leggibili come data", c=label, n=d["unparsable"]),
                                     "Quelle righe non genereranno l'evento corrispondente.",
                                     "Controlla il formato (es. date con testo, formati misti) nell'estrazione."))
            reason = planned.get(name, {}).get(col)
            if reason:
                days = _days_column(df, col)
                if days:
                    t.setdefault("planned_days", {})[col] = days
                issues.append(_planned_issue(df, name, col, reason, due_objectives or [], days))
            elif d["future"]:
                issues.append(_issue("attenzione", name, msg("{c}: {n} date nel futuro", c=label, n=d["future"]),
                                     "Un evento è qualcosa che è già successo. Date nel futuro di solito sono date pianificate "
                                     "(consegna prevista, scadenza di pagamento): usate come eventi metterebbero nel processo "
                                     "passi che non sono ancora avvenuti.",
                                     "Se la colonna è una data prevista: nella revisione del mapping rifiuta la riga che la usa "
                                     "come data/ora di un evento (oppure, con «Modifica», trasformala in attributo, così resta "
                                     "disponibile per l'analisi, es. confronto tra consegna prevista e reale). Se invece sono fatti "
                                     "già avvenuti, le date sono sbagliate: segnalalo a chi ha fatto l'estrazione."))
            if d["midnight_pct"] is not None and d["midnight_pct"] >= 50:
                issues.append(_issue("attenzione", name, msg("{c}: {p}% degli orari è 00:00:00", c=label, p=d["midnight_pct"]),
                                     "Probabile registrazione batch o ora non significativa: i tempi tra attività calcolati al minuto sarebbero fittizi.",
                                     "Chiedi se esiste un campo con l'ora reale (es. ora di creazione separata) e includilo nell'estrazione."))
            if d["outside_period"]:
                issues.append(_issue("attenzione", name, msg("{c}: {n} date fuori dal perimetro dell'assessment", c=label, n=d["outside_period"]),
                                     "L'estrazione non è filtrata come previsto o il perimetro dichiarato è diverso: i confronti tra periodi saranno falsati.",
                                     "Verifica i filtri dell'estrazione o aggiorna il periodo nell'assessment."))

    if any_time_info and not any(any_time_info):
        issues.append(_issue("attenzione", None, "Nessuna colonna data contiene l'ora",
                             "Gli eventi dello stesso giorno avranno un ordine arbitrario e non si potranno misurare tempi inferiori al giorno: varianti e loop possono risultare inventati.",
                             "Se il sistema registra l'ora (es. campi ora separati come ERZET/CPUTM in SAP), includila nell'estrazione."))

    # periodo coperto rispetto al perimetro
    if (p_from is not None and not pd.isna(p_from)) or (p_to is not None and not pd.isna(p_to)):
        for t in tables:
            mins = [d["min"] for d in t["dates"] if d["min"]]
            maxs = [d["max"] for d in t["dates"] if d["max"]]
            if not mins:
                continue
            tmin, tmax = pd.Timestamp(min(mins)), pd.Timestamp(max(maxs))
            gaps, missing_days = [], 0
            if p_from is not None and not pd.isna(p_from) and tmin > p_from + pd.Timedelta(days=31):
                gaps.append(msg("inizia il {d}", d=f"{tmin:%Y-%m-%d}"))
                missing_days += (tmin - p_from).days
            if p_to is not None and not pd.isna(p_to) and tmax < p_to - pd.Timedelta(days=31):
                gaps.append(msg("finisce il {d}", d=f"{tmax:%Y-%m-%d}"))
                missing_days += (p_to - tmax).days
            if gaps:
                span = (p_to - p_from).days if p_from is not None and p_to is not None and not pd.isna(p_from) and not pd.isna(p_to) else 0
                severity = "attenzione" if span and missing_days > span / 4 else "info"
                issues.append(_issue(severity, t["name"], (msg("Copertura parziale del periodo: la tabella {a} e {b}", a=gaps[0], b=gaps[1]) if len(gaps) == 2
                                      else msg("Copertura parziale del periodo: la tabella {a}", a=gaps[0])),
                                     "Per una parte del perimetro mancheranno gli eventi di questa tabella: il processo sembrerà interrompersi.",
                                     "Verifica che l'estrazione copra tutto il periodo dichiarato nell'assessment."))

    relationships = _find_relationships(frames, {t["name"]: t["key"] for t in tables})

    for t in tables:
        df = frames[t["name"]]
        dates = {d["column"] for d in t["dates"]}
        t["activity_columns"] = _activity_candidates(df, dates, t["key"], relationships, t["name"])
        for a in t["activity_columns"]:
            shown = ", ".join(a["values"][:6]) + ("…" if len(a["values"]) > 6 else "")
            issues.append(_issue(
                "attenzione" if a["name_hint"] else "info", t["name"],
                msg("{c}: probabile colonna attività ({n} valori: {v})", c=a["column"], n=len(a["values"]), v=shown),
                "Le righe di questa tabella sembrano operazioni diverse distinte da questa colonna. Se diventano "
                "un'unica attività, passi diversi del processo si confondono e tempi e varianti risultano sbagliati.",
                "Nel mapping verrà proposta come colonna attività: ogni valore diventa un'attività con un nome "
                "leggibile, da confermare (o escludere) in revisione.",
            ))
        t["reason_columns"] = _reason_columns(df, t["key"])
        for rc in t["reason_columns"]:
            shown = ", ".join(rc["values"][:6]) + ("…" if len(rc["values"]) > 6 else "")
            issues.append(_issue(
                "info", t["name"],
                msg("{c}: probabile motivo o causale ({n} valori: {v})", c=rc["column"], n=len(rc["values"]), v=shown),
                "Dice perché è stata fatta un'operazione, non quale: usata come attività mescolerebbe i passi del "
                "processo con le loro motivazioni.",
                ("Nel mapping verrà proposta come attributo dell'evento: servirà a filtrare e confrontare i casi per "
                 "motivo (es. quanti blocchi, rifiuti o rettifiche per ciascun motivo)." if dates else
                 "Nel mapping verrà proposta come attributo dell'oggetto: servirà a filtrare e confrontare i casi per motivo."),
            ))
    for r in relationships:
        if r["coverage_pct"] < COVERAGE_WARN * 100:
            issues.append(_issue("attenzione", r["child_table"],
                                 msg("{a} → {b}: {n} righe senza corrispondenza ({p}% collegate)",
                                     a=f"{r['child_table']}.{r['child_column']}", b=f"{r['parent_table']}.{r['parent_column']}",
                                     n=r["orphan_rows"], p=r["coverage_pct"]),
                                 "Le righe orfane producono eventi non collegati al loro oggetto principale: casi incompleti e tempi sbagliati.",
                                 "Spesso le tabelle sono estratte con filtri o periodi diversi: allinea i filtri (stesse società, stesso periodo)."))
    linked = {r["child_table"] for r in relationships} | {r["parent_table"] for r in relationships}
    if len(frames) > 1:
        for name in frames:
            if name not in linked and len(frames[name]):
                issues.append(_issue("attenzione", name, "Tabella isolata: nessun collegamento trovato con le altre",
                                     "I suoi eventi non potranno essere legati agli oggetti del processo.",
                                     "Includi nell'estrazione la colonna che la collega alle altre tabelle (es. numero documento di riferimento) o descrivi il collegamento nel passo successivo."))

    # tabelle che ripetono le righe di un'altra (indici, viste, export filtrati): un solo avviso per tabella
    copies = _find_copies(frames)
    for cp in copies:
        issues = [i for i in issues if i["table"] != cp["table"] or i["severity"] == "bloccante"]
        title = (msg("Ripete la tabella {a}: tutte le sue {n} righe sono già lì", a=cp["of"], n=cp["rows"])
                 if cp["share"] >= 1 else
                 msg("Ripete in gran parte la tabella {a}: {p}% delle sue righe sono già lì", a=cp["of"], p=round(100 * cp["share"])))
        impact = msg("Mappata insieme a {a} genererebbe due volte gli stessi oggetti ed eventi: conteggi doppi e oggetti "
                     "in più che in realtà sono gli stessi. Succede con tabelle indice, viste o estrazioni filtrate "
                     "della stessa tabella (in SAP, ad esempio, BSAD, BSID, BSAK e BSIK ripetono righe di BSEG).", a=cp["of"])
        if cp["exclude"]:
            extra = (msg("Le sue colonne in più ({c}) ci sono già in altre tabelle.", c=", ".join(cp["extra"]))
                     if cp["extra"] else msg("Non ha colonne che {a} non abbia.", a=cp["of"]))
            action = concat(extra, msg("Per questo verrà esclusa dal mapping: puoi rimetterla con la casella in fondo alla pagina."))
        else:
            action = msg("Ha colonne che le altre tabelle non hanno ({c}): resta nel mapping solo per quelle, come "
                         "attributi; oggetti ed eventi si prendono da {a}. Se non ti servono, escludila con la casella "
                         "in fondo alla pagina.", c=", ".join(cp["new"]), a=cp["of"])
        issues.append(_issue("attenzione", cp["table"], title, impact, action))
    issues.sort(key=lambda i: (SEVERITY_ORDER[i["severity"]], i["table"] or ""))
    counts = {s: sum(1 for i in issues if i["severity"] == s) for s in SEVERITY_ORDER}
    if counts["bloccante"]:
        verdict = ("bloccante", "Conviene correggere i dati prima di proseguire")
    elif counts["attenzione"]:
        verdict = ("attenzione", "Si può proseguire, ma ci sono punti da verificare")
    else:
        verdict = ("ok", "Dati pronti per il mapping")
    return {"tables": tables, "relationships": relationships, "issues": issues, "counts": counts, "copies": copies,
            "verdict": {"level": verdict[0], "label": verdict[1]},
            "total_rows": sum(t["rows"] for t in tables)}


def _activity_candidates(df: pd.DataFrame, dates: set[str], key: list[str] | None,
                         relationships: list[dict], table: str) -> list[dict]:
    """Colonne che con buona probabilita' dicono quale operazione e' registrata in
    ogni riga. Servono tre cose: una data (la riga e' un evento), una colonna con
    poche modalita' quasi sempre valorizzata, e una tabella che si comporta da log
    (senza chiave propria, oppure con piu' righe per lo stesso oggetto collegato).
    Il nome della colonna (stato, azione, tipo movimento...) e' un indizio forte;
    senza nome indicativo contano solo valori testuali (etichette, non codici)."""
    n = len(df)
    if not dates or n < 4:
        return []
    repeated_link = any(
        r["child_table"] == table and df[r["child_column"].split("+")].duplicated().any()
        for r in relationships if all(c in df.columns for c in r["child_column"].split("+"))
    )
    if key and not repeated_link:
        return []  # una riga per oggetto: uno stato qui e' un attributo, non una sequenza di attivita'
    links = {col for r in relationships if r["child_table"] == table for col in r["child_column"].split("+")}
    found = []
    for i, c in enumerate(df.columns):
        if c in dates or (key and c in key):
            continue
        strong = bool(STRONG_ACTIVITY_RE.search(c))
        # EVENT_NAME, ACTIVITY_NAME: «name» qui e' il nome dell'operazione, non di una persona
        if USER_HINT_RE.search(re.sub(r"(name|nome)", "", c, flags=re.I) if strong else c):
            continue
        if REASON_HINT_RE.search(c) and not strong:
            continue  # il motivo dice perche', non che cosa: vedi _reason_columns
        name_hint = bool(ACTIVITY_HINT_RE.search(c))
        if not name_hint and (c in links or ID_HINT_RE.search(c)):
            continue  # riferimento a un oggetto (numero ordine, id caso...), non un'operazione
        s = df[c].dropna().astype(str).str.strip()
        s = s[s != ""]
        if len(s) < 0.9 * n:
            continue
        if looks_like_time(c, s):
            continue  # un'ora (es. EVENT_TIME, CPUTM) dice quando, non cosa: si unisce alla sua data
        distinct = s.unique()
        if not 2 <= len(distinct) <= MAX_ACTIVITY_VALUES or len(distinct) > 0.5 * n:
            continue
        if s.str.match(DECIMAL_RE).mean() > 0.5:
            continue
        labels = s.str.contains(r"[A-Za-zÀ-ÿ]").mean() > 0.9 and s.str.len().mean() >= 4
        if not (name_hint or labels):
            continue
        counts = s.value_counts()
        found.append(((0 if strong else 1 if name_hint else 2, i),
                      {"column": c, "name_hint": name_hint, "values": [str(v) for v in counts.index],
                       "counts": {str(k): int(v) for k, v in counts.items()}}))
    out = []
    # due colonne che si determinano a vicenda (es. EKBE.VGABE e BEWTP) dicono la stessa cosa: si segnala
    # solo quella col nome piu' da attivita' (EVENT_NAME prima di STATUS), a parita' la prima
    for _, cand in sorted(found, key=lambda x: x[0]):
        if not any(_same_partition(df, cand["column"], o["column"]) for o in out):
            out.append(cand)
    out.sort(key=lambda o: list(df.columns).index(o["column"]))
    # con una colonna dal nome indicativo, le altre (solo etichette testuali) sono quasi sempre dimensioni
    if any(o["name_hint"] for o in out):
        out = [o for o in out if o["name_hint"]]
    return out


def _reason_columns(df: pd.DataFrame, key: list[str] | None) -> list[dict]:
    """Colonne con il motivo o la causale di un'operazione (motivo di blocco, di rifiuto, di rettifica...):
    poche modalita', anche se valorizzate solo su parte delle righe. Sono attributi utili per filtrare e
    confrontare, non attivita'."""
    out = []
    for c in df.columns:
        if (key and c in key) or not REASON_HINT_RE.search(c) or STRONG_ACTIVITY_RE.search(c):
            continue
        s = df[c].dropna().astype(str).str.strip()
        s = s[s != ""]
        if s.empty:
            continue
        counts = s.value_counts()
        if len(counts) > MAX_ACTIVITY_VALUES:
            continue
        out.append({"column": c, "values": [str(v) for v in counts.index],
                    "filled_pct": round(100 * len(s) / len(df))})
    return out


def _find_copies(frames: dict[str, pd.DataFrame]) -> list[dict]:
    """Tabelle le cui righe (sulle colonne in comune con un'altra tabella) sono quasi tutte gia' in
    quell'altra tabella: indici o viste (SAP BSAD/BSID su BSEG), export filtrati, stessa tabella caricata
    due volte. Servono molte colonne in comune (almeno 3 e almeno il 60% delle sue), e non solo chiavi:
    anche la maggior parte dei contenuti (importi, date...). Se le sue colonne in
    piu' esistono gia' in altre tabelle (o non ne ha), non aggiunge nulla e si propone di escluderla."""
    def norm(df, cols):
        return df[cols].astype(str).apply(lambda s: s.str.strip()).replace({"nan": "", "None": ""}).agg("|".join, axis=1)

    names = list(frames)
    lower_cols = {n: {c.lower(): c for c in frames[n].columns} for n in names}
    out, taken = [], set()
    for b in names:
        db = frames[b]
        if len(db) < 2:
            continue
        best = None
        for a in names:
            if a == b or a in taken or len(frames[a]) < len(db):
                continue
            common = [c for c in db.columns if c.lower() in lower_cols[a]]
            if len(common) < 3 or len(common) < 0.6 * len(db.columns):
                continue
            # in comune non solo chiavi e riferimenti (testata e posizioni li condividono sempre),
            # ma anche i contenuti: importi, date, condizioni (colonne non costanti)
            content = [c for c in db.columns if not ID_HINT_RE.search(c) and db[c].nunique() > 1]
            shared = [c for c in content if c in common]
            if len(shared) < 2 or len(shared) < 0.5 * len(content):
                continue
            da = frames[a]
            in_a = set(norm(da, [lower_cols[a][c.lower()] for c in common]))
            share = float(norm(db, common).isin(in_a).mean())
            # due tabelle identiche: e' la seconda caricata a ripetere la prima
            if len(da) == len(db) and names.index(a) > names.index(b):
                continue
            if share >= 0.95 and (best is None or share > best[1]):
                best = (a, share, common)
        if not best:
            continue
        a, share, common = best
        extra = [c for c in db.columns if c not in common]
        elsewhere = {c.lower() for n in names if n != b for c in frames[n].columns}
        new = [c for c in extra if c.lower() not in elsewhere and db[c].notna().any()]
        out.append({"table": b, "of": a, "share": share, "rows": len(db), "extra": extra, "new": new,
                    "exclude": not new})
        taken.add(b)
    return out


def _days_column(df: pd.DataFrame, exclude: str) -> str | None:
    """Colonna della stessa tabella con un numero di giorni da sommare a una data base (es. giorni di
    pagamento): nome indicativo e valori interi tra 0 e 400."""
    for c in df.columns:
        if c == exclude or not DAYS_HINT_RE.search(c):
            continue
        s = df[c].dropna().astype(str).str.strip()
        s = s[s != ""]
        if s.empty:
            continue
        num = pd.to_numeric(s, errors="coerce")
        if num.notna().mean() >= 0.9 and ((num.dropna() % 1) == 0).all() and num.min() >= 0 and num.max() <= 400:
            return c
    return None


def _planned_issue(df: pd.DataFrame, table: str, col: str, reason, due_objectives: list[str], days: str | None) -> dict:
    """Data prevista o di scadenza: non e' un evento, ma con i giorni da aggiungere (se ci sono) da' la
    scadenza, il riferimento per misurare i ritardi."""
    impact = msg("Una data così è stabilita in anticipo, non registra qualcosa che è successo: come evento "
                 "metterebbe nel processo un passo che nessuno ha svolto e falserebbe sequenze e tempi. "
                 "Indizio: {r}.", r=reason)
    if days:
        action = msg("Nel mapping non va usata come data di un evento: tienila come attributo insieme a {d} "
                     "(numero di giorni). {c} + {d} dà probabilmente la scadenza, il riferimento per dire se "
                     "un pagamento o una consegna è arrivata in ritardo.", c=col, d=days)
    else:
        action = msg("Nel mapping non va usata come data di un evento: tienila come attributo (resta utile, "
                     "es. per sapere se un pagamento o una consegna è arrivata in ritardo).")
    goal = (msg("È il dato che serve all'obiettivo dell'assessment «{o}».", o=msg(due_objectives[0]))
            if due_objectives else None)
    return _issue("attenzione", table, msg("{c}: probabile data prevista o di scadenza", c=col), impact,
                  concat(action, goal, msg("Le proposte che la usano come evento saranno segnalate come "
                                           "incerte in revisione.")))


def _same_partition(df: pd.DataFrame, a: str, b: str) -> bool:
    pair = df[[a, b]].dropna().astype(str)
    return bool(len(pair)) and pair.groupby(a)[b].nunique().max() == 1 and pair.groupby(b)[a].nunique().max() == 1


def _find_relationships(frames: dict[str, pd.DataFrame], keys: dict[str, list[str] | None]) -> list[dict]:
    """Collegamenti figlio→padre: il padre e' la chiave (anche composta) della sua
    tabella; il figlio e' una colonna (o coppia di colonne con gli stessi nomi)
    di un'altra tabella i cui valori stanno nella chiave del padre. Colonne con
    nome diverso contano solo se gran parte dei loro valori sta nel padre."""
    def tuples(df, cols):
        sub = df[cols].dropna()
        return sub.astype(str).agg("|".join, axis=1) if len(cols) > 1 else sub[cols[0]].astype(str)

    rels = []
    for pname, pkey in keys.items():
        if not pkey:
            continue
        pdf = frames[pname]
        pvals = set(tuples(pdf, pkey))
        for cname, cdf in frames.items():
            if cname == pname:
                continue
            lower = {c.lower(): c for c in cdf.columns}
            if len(pkey) > 1:
                if not all(k.lower() in lower for k in pkey):
                    continue
                candidates = [([lower[k.lower()] for k in pkey], True)]
            else:
                candidates = [([c], c.lower() == pkey[0].lower()) for c in cdf.columns]
            for ccols, same_name in candidates:
                s = tuples(cdf, ccols)
                if s.empty or (not same_name and s.nunique() < 2):
                    continue
                distinct = set(s.unique())
                containment = len(distinct & pvals) / len(distinct)
                if containment == 0 or (not same_name and containment < MIN_LINK_CONTAINMENT):
                    continue
                if keys.get(cname) == ccols:
                    continue  # chiave contro chiave: tabelle 1:1, non un riferimento
                orphan_rows = int((~s.isin(pvals)).sum())
                rels.append({
                    "child_table": cname, "child_column": "+".join(ccols),
                    "parent_table": pname, "parent_column": "+".join(pkey),
                    "coverage_pct": round(100 * (len(s) - orphan_rows) / len(s), 1), "orphan_rows": orphan_rows,
                    "matched_by": "nome e valori" if same_name else "valori",
                })
    # per ogni colonna figlio tiene il collegamento migliore; una colonna gia'
    # coperta da un collegamento a chiave composta non si ripete da sola
    best: dict[tuple[str, str], dict] = {}
    for r in rels:
        k = (r["child_table"], r["child_column"])
        score = (r["matched_by"] != "valori", r["coverage_pct"])
        if k not in best or score > (best[k]["matched_by"] != "valori", best[k]["coverage_pct"]):
            best[k] = r
    return sorted(best.values(), key=lambda r: (r["child_table"], r["child_column"]))


def compact_for_mapping(profile: dict) -> dict:
    """Sintesi misurata da dare all'AI Mapping: chiavi, colonne data, collegamenti."""
    return {
        "candidate_keys": {t["name"]: t["key"] for t in profile["tables"] if t["key"]},
        "date_columns": {
            t["name"]: {d["column"]: {"has_time": d["has_time"], "filled_pct": d["filled_pct"], "range": [d["min"], d["max"]]}
                        for d in t["dates"]}
            for t in profile["tables"] if t["dates"]
        },
        "relationships": [
            f"{r['child_table']}.{r['child_column']} -> {r['parent_table']}.{r['parent_column']} ({r['coverage_pct']}% collegate)"
            for r in profile["relationships"]
        ],
        # date previste o di scadenza (non fatti avvenuti): mai la data di un evento
        # con i giorni da sommare, se ci sono: data + giorni = scadenza (entrambe attributi)
        "planned_dates": {t["name"]: {c: render("it", r) + (f"; scadenza = {c} + {t['planned_days'][c]} (giorni), mappa anche {t['planned_days'][c]} come attributo"
                                                             if c in t.get("planned_days", {}) else "")
                                      for c, r in t["planned_dates"].items()}
                          for t in profile["tables"] if t.get("planned_dates")},
        # colonne che distinguono operazioni diverse nella stessa tabella: tutti i valori distinti
        "activity_columns": {
            t["name"]: {a["column"]: a["values"] for a in t.get("activity_columns", [])}
            for t in profile["tables"] if t.get("activity_columns")
        },
        # motivo o causale di un'operazione: attributo, mai attivita'
        "reason_columns": {
            t["name"]: {r["column"]: r["values"] for r in t.get("reason_columns", [])}
            for t in profile["tables"] if t.get("reason_columns")
        },
        # tabelle che ripetono le righe di un'altra: oggetti ed eventi vengono dall'altra
        "copy_of": {c["table"]: {"of": c["of"], "new_columns": c["new"]} for c in profile.get("copies", [])},
    }
