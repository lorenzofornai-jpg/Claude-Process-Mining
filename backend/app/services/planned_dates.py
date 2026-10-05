"""Date previste o di scadenza: date stabilite in anticipo, non fatti avvenuti.

Una scadenza di pagamento, una consegna prevista o la data base da cui si
calcola la scadenza (SAP ZFBDT) sono scritte sul documento: in quel giorno
nessuno fa niente. Usate come data di un evento metterebbero nel processo un
passo mai svolto, falsando sequenze, tempi e varianti. Vanno tenute come
attributi (utilissime per l'analisi: pagato in ritardo? consegnato in tempo?).

Si riconoscono, senza AI, da tre indizi:
1. il nome: codici SAP noti (ZFBDT, NETDT, EINDT...) o parole come scadenza,
   due, planned, previsto, requested, valid_to;
2. molte date nel futuro rispetto a oggi;
3. una distanza fissa da un'altra data della stessa riga (es. sempre 30, 60 o
   90 giorni dopo la data documento): e' una data calcolata da una regola.
"""
from __future__ import annotations

import re
import warnings
from datetime import datetime

import pandas as pd

from app.i18n import msg

SAP_PLANNED = {
    "ZFBDT": "data base per il calcolo della scadenza (SAP)",
    "NETDT": "data di scadenza netta (SAP)",
    "FAEDT": "data di scadenza (SAP)",
    "EINDT": "data di consegna prevista (SAP)",
    "SLFDT": "data di consegna statistica (SAP)",
    "LFDAT": "data di consegna prevista (SAP)",
    "VDATU": "data di consegna richiesta dal cliente (SAP)",
    "WADAT": "data prevista di uscita merci (SAP; quella effettiva è WADAT_IST)",
    "MBDAT": "data di disponibilità del materiale (SAP)",
    "KDATB": "inizio validità (SAP)",
    "KDATE": "fine validità (SAP)",
    "DATAB": "inizio validità (SAP)",
    "DATBI": "fine validità (SAP)",
}
NAME_RE = re.compile(
    r"(due|scaden|deadline|expir|planned|^plan_|_plan|previst|expected|target|promised|requested|richiest|"
    r"baseline|valid_?(from|to|until)|validit|forecast|estimated|stimat|^eta$|^eta_|_eta$)",
    re.I,
)
MIN_FUTURE_SHARE = 0.2
MIN_ROWS_FOR_OFFSET = 10
MIN_OFFSET_DAYS = 7


def _parse(s: pd.Series) -> pd.Series:
    s = s.dropna().astype(str).str.strip()
    s = s[(s != "") & ~s.str.match(r"^(0{8}|9999)")]
    if len(s) and s.str.fullmatch(r"\d{8}").all():
        return pd.to_datetime(s, format="%Y%m%d", errors="coerce")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return pd.to_datetime(s, errors="coerce")


def planned_reasons(df: pd.DataFrame, date_columns: list[str], today: datetime | None = None) -> dict:
    """{colonna data: motivo (testo o messaggio da tradurre)} per le colonne che sembrano date previste o di scadenza."""
    today = pd.Timestamp(today or datetime.now())
    parsed = {c: _parse(df[c]) for c in date_columns if c in df.columns}
    out: dict[str, str] = {}
    for col, dates in parsed.items():
        up = col.upper()
        if up in SAP_PLANNED:
            out[col] = SAP_PLANNED[up]
            continue
        if NAME_RE.search(col):
            out[col] = "il nome indica una data prevista o di scadenza"
            continue
        ok = dates.dropna()
        if len(ok):
            future = int((ok > today).sum())
            if future >= 3 and future / len(ok) >= MIN_FUTURE_SHARE:
                out[col] = msg("il {p}% delle date è nel futuro", p=round(100 * future / len(ok)))
                continue
        for other, other_dates in parsed.items():
            if other == col:
                continue
            both = pd.concat([ok, other_dates.dropna()], axis=1, join="inner")
            if len(both) < MIN_ROWS_FOR_OFFSET:
                continue
            days = (both.iloc[:, 0] - both.iloc[:, 1]).dt.days
            top = days.value_counts().head(3)
            # solo la data che viene DOPO: e' quella calcolata (la scadenza), non quella di partenza
            if top.sum() / len(days) >= 0.9 and all(d >= MIN_OFFSET_DAYS for d in top.index):
                offsets = ", ".join(str(int(d)) for d in sorted(top.index))
                out[col] = msg("è sempre {d} giorni dopo {o}: una data calcolata da una regola (es. condizioni di "
                               "pagamento)", d=offsets, o=other)
                break
    return out
