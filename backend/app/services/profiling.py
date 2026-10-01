"""Profilazione deterministica dei dati caricati (nessuna chiamata AI).

Gira subito dopo l'upload, prima di controllo di pertinenza e mapping AI, e
risponde alle domande che di solito emergono solo a progetto avanzato:
- ogni tabella ha una chiave univoca? ci sono righe duplicate?
- le tabelle si collegano tra loro? quante righe restano orfane?
- le date sono utilizzabili (ora presente, valori segnaposto, futuro, batch a
  mezzanotte) e coprono il perimetro dichiarato nell'assessment?

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
) -> dict:
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
             "empty_columns": [], "constant_columns": [], "dates": []}
        tables.append(t)
        if n == 0:
            issues.append(_issue("bloccante", name, "La tabella è vuota",
                                 "Non produrrà né oggetti né eventi: la parte di processo che rappresenta mancherà nel log.",
                                 "Verifica i filtri dell'estrazione e ricarica il file."))
            continue

        t["duplicates"] = int(df.duplicated().sum())
        if t["duplicates"]:
            issues.append(_issue("attenzione", name, f"{t['duplicates']} righe duplicate identiche",
                                 "Gli eventi duplicati gonfiano conteggi e frequenze e creano finti ricicli nel processo.",
                                 "Controlla se l'estrazione ha unito più volte gli stessi dati; i duplicati esatti si possono scartare."))
        t["empty_columns"] = [c for c in df.columns if df[c].isna().all()]
        t["constant_columns"] = [c for c in df.columns if c not in t["empty_columns"] and df[c].nunique() == 1]
        if t["empty_columns"]:
            issues.append(_issue("info", name, f"Colonne sempre vuote: {', '.join(t['empty_columns'])}",
                                 "Non portano informazione al log.",
                                 "Si possono togliere dall'estrazione; se dovrebbero essere valorizzate, verifica il campo sorgente."))
        t["key"] = _find_key(df, set(date_columns.get(name, [])))
        if t["key"] is None and not t["duplicates"]:
            issues.append(_issue("attenzione", name, "Nessuna chiave univoca trovata (né una colonna né una coppia)",
                                 "Senza una chiave le righe non si possono identificare come oggetti distinti, né collegare in modo affidabile ad altre tabelle.",
                                 "Indica nella descrizione della tabella quali colonne la identificano, o includi nell'estrazione il campo chiave mancante (es. numero posizione)."))

        for col in date_columns.get(name, []):
            if col not in df.columns:
                continue
            raw = df[col]
            parsed, placeholder, placeholder_values = _parse_dates(raw)
            nonnull = int(raw.notna().sum())
            ok = parsed.dropna()
            d = {"column": col, "filled_pct": round(100 * nonnull / n), "min": None, "max": None,
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

            label = col  # la tabella e' gia' indicata accanto al titolo
            if d["placeholders"]:
                issues.append(_issue("attenzione", name, f"{label}: {d['placeholders']} date segnaposto ({', '.join(placeholder_values)})",
                                     "Sono valori convenzionali, non date reali: se usate come istante dell'evento falsano tempi e ordinamento.",
                                     "Di solito significano \"non ancora avvenuto\": verranno trattate come date mancanti, l'evento non sarà generato per quelle righe."))
            if d["unparsable"]:
                issues.append(_issue("attenzione", name, f"{label}: {d['unparsable']} valori non leggibili come data",
                                     "Quelle righe non genereranno l'evento corrispondente.",
                                     "Controlla il formato (es. date con testo, formati misti) nell'estrazione."))
            if d["future"]:
                issues.append(_issue("attenzione", name, f"{label}: {d['future']} date nel futuro",
                                     "Eventi nel futuro sono di solito date pianificate (es. consegna prevista), non eventi avvenuti: inserirli come eventi altera il processo.",
                                     "Se la colonna è una data prevista, trattala come attributo e non come evento."))
            if d["midnight_pct"] is not None and d["midnight_pct"] >= 50:
                issues.append(_issue("attenzione", name, f"{label}: {d['midnight_pct']}% degli orari è 00:00:00",
                                     "Probabile registrazione batch o ora non significativa: i tempi tra attività calcolati al minuto sarebbero fittizi.",
                                     "Chiedi se esiste un campo con l'ora reale (es. ora di creazione separata) e includilo nell'estrazione."))
            if d["outside_period"]:
                issues.append(_issue("attenzione", name, f"{label}: {d['outside_period']} date fuori dal perimetro dell'assessment",
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
                gaps.append(f"inizia il {tmin:%Y-%m-%d}")
                missing_days += (tmin - p_from).days
            if p_to is not None and not pd.isna(p_to) and tmax < p_to - pd.Timedelta(days=31):
                gaps.append(f"finisce il {tmax:%Y-%m-%d}")
                missing_days += (p_to - tmax).days
            if gaps:
                span = (p_to - p_from).days if p_from is not None and p_to is not None and not pd.isna(p_from) and not pd.isna(p_to) else 0
                severity = "attenzione" if span and missing_days > span / 4 else "info"
                issues.append(_issue(severity, t["name"], f"Copertura parziale del periodo: la tabella {' e '.join(gaps)}",
                                     "Per una parte del perimetro mancheranno gli eventi di questa tabella: il processo sembrerà interrompersi.",
                                     "Verifica che l'estrazione copra tutto il periodo dichiarato nell'assessment."))

    relationships = _find_relationships(frames, {t["name"]: t["key"] for t in tables})
    for r in relationships:
        if r["coverage_pct"] < COVERAGE_WARN * 100:
            issues.append(_issue("attenzione", r["child_table"],
                                 f"{r['child_table']}.{r['child_column']} → {r['parent_table']}.{r['parent_column']}: "
                                 f"{r['orphan_rows']} righe senza corrispondenza ({r['coverage_pct']}% collegate)",
                                 "Le righe orfane producono eventi non collegati al loro oggetto principale: casi incompleti e tempi sbagliati.",
                                 "Spesso le tabelle sono estratte con filtri o periodi diversi: allinea i filtri (stesse società, stesso periodo)."))
    linked = {r["child_table"] for r in relationships} | {r["parent_table"] for r in relationships}
    if len(frames) > 1:
        for name in frames:
            if name not in linked and len(frames[name]):
                issues.append(_issue("attenzione", name, "Tabella isolata: nessun collegamento trovato con le altre",
                                     "I suoi eventi non potranno essere legati agli oggetti del processo.",
                                     "Includi nell'estrazione la colonna che la collega alle altre tabelle (es. numero documento di riferimento) o descrivi il collegamento nel passo successivo."))

    issues.sort(key=lambda i: (SEVERITY_ORDER[i["severity"]], i["table"] or ""))
    counts = {s: sum(1 for i in issues if i["severity"] == s) for s in SEVERITY_ORDER}
    if counts["bloccante"]:
        verdict = ("bloccante", "Conviene correggere i dati prima di proseguire")
    elif counts["attenzione"]:
        verdict = ("attenzione", "Si può proseguire, ma ci sono punti da verificare")
    else:
        verdict = ("ok", "Dati pronti per il mapping")
    return {"tables": tables, "relationships": relationships, "issues": issues, "counts": counts,
            "verdict": {"level": verdict[0], "label": verdict[1]},
            "total_rows": sum(t["rows"] for t in tables)}


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
    }
