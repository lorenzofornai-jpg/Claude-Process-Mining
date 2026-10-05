"""Colonne "solo ora" e abbinamento data + ora sulla stessa riga.

Molti sistemi registrano giorno e ora in due campi separati: in SAP
CPUDT + CPUTM (data/ora di registrazione), ERDAT + ERZET (creazione),
AEDAT + AEZET (modifica), UDATE + UTIME (change log); altrove
created_date + created_time, data_ordine + ora_ordine e simili.

Una colonna di sole ore non e' una data: letta come data diventerebbe "oggi
a quell'ora" (date finte, spesso nel futuro). Va riconosciuta come ora e
unita alla sua colonna data, cosi' l'evento ha l'istante completo invece
del solo giorno.
"""
from __future__ import annotations

import re

import pandas as pd

TIME_TEXT_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d(:[0-5]\d)?(\.\d+)?$")
TIME_DIGITS_RE = re.compile(r"^\d{5,6}$")
# nomi da colonna ora: senza un nome cosi' un numero a 6 cifre (es. 143522) resta un numero
TIME_NAME_RE = re.compile(r"(time|ora|orario|uhrzeit|zeit|_tm$|tm$|^cputm|^erzet|^aezet|^utime|^uzeit|zet$)", re.I)

# coppie SAP note (ora -> data); con suffisso (es. CPUTM_MKPF) vale lo stesso suffisso
SAP_PAIRS = {"CPUTM": "CPUDT", "ERZET": "ERDAT", "AEZET": "AEDAT", "UTIME": "UDATE", "UZEIT": "DATUM",
             "ERFZEIT": "ERFDATE", "LTIME": "LDATE"}

_DATE_WORDS = ("date", "data", "datum", "_dt", "dat", "day", "giorno")
_TIME_WORDS = ("time", "ora", "orario", "zeit", "_tm", "tm", "zet", "hour")


def parse_time_of_day(raw) -> tuple[int, int, int] | None:
    """(ore, minuti, secondi) da "14:35:22", "14:35", "143522" o "93522" (zero iniziale perso)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if s in ("", "nan"):
        return None
    if TIME_TEXT_RE.match(s):
        parts = s.split(".")[0].split(":")
        h, m = int(parts[0]), int(parts[1])
        sec = int(parts[2]) if len(parts) > 2 else 0
        return h, m, sec
    if s.endswith(".0"):  # intero letto come float
        s = s[:-2]
    if TIME_DIGITS_RE.match(s):
        s = s.zfill(6)
        h, m, sec = int(s[:2]), int(s[2:4]), int(s[4:])
        if h < 24 and m < 60 and sec < 60:
            return h, m, sec
    return None


def looks_like_time(name: str, non_null: pd.Series) -> bool:
    """Colonna di sole ore? Testo "hh:mm[:ss]" sempre; cifre (hhmmss) solo con un nome da ora."""
    s = non_null.astype(str).str.strip()
    s = s[s != ""]
    if s.empty:
        return False
    if s.str.match(TIME_TEXT_RE).mean() > 0.95:
        return True
    if TIME_NAME_RE.search(name):
        valid = s.map(lambda v: parse_time_of_day(v) is not None)
        return bool(valid.mean() > 0.95)
    return False


def _stem(name: str, words: tuple[str, ...]) -> str | None:
    low = name.lower()
    for w in sorted(words, key=len, reverse=True):
        if low.endswith(w):
            return low[: -len(w)].rstrip("_- ")
        if low.startswith(w):
            return low[len(w):].lstrip("_- ")
    return None


def pair_time_columns(date_columns: list[str], time_columns: list[str]) -> dict[str, str]:
    """{colonna data: colonna ora} per le colonne di una stessa tabella.

    1. coppie SAP note (anche con lo stesso suffisso: CPUDT_MKPF + CPUTM_MKPF);
    2. stesso nome a parte la parola data/ora (created_date + created_time);
    3. una sola colonna data e una sola colonna ora nella tabella."""
    pairs: dict[str, str] = {}
    free_dates = list(date_columns)
    upper_dates = {d.upper(): d for d in date_columns}
    for t in time_columns:
        up = t.upper()
        for time_code, date_code in SAP_PAIRS.items():
            if up == time_code or up.startswith(time_code + "_"):
                target = upper_dates.get(date_code + up[len(time_code):])
                if target and target in free_dates:
                    pairs[target] = t
                    free_dates.remove(target)
                break
    for t in time_columns:
        if t in pairs.values():
            continue
        stem = _stem(t, _TIME_WORDS)
        if stem is None:
            continue
        match = next((d for d in free_dates if _stem(d, _DATE_WORDS) == stem), None)
        if match:
            pairs[match] = t
            free_dates.remove(match)
    if not pairs and len(date_columns) == 1 and len(time_columns) == 1:
        pairs[date_columns[0]] = time_columns[0]
    return pairs
