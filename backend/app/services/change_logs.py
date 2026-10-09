"""Log di modifiche: tabelle con campo modificato, valore vecchio e valore nuovo (SAP CDHDR/CDPOS, audit trail,
cronologia dei campi di un CRM). L'attivita' non sta in una colonna: si ricava da tre.

- detect(df): riconosce le tre colonne dal nome e conta le modifiche per tipo e campo
  (SET = impostato, vecchio vuoto; REMOVE = tolto, nuovo vuoto; CHANGE = cambiato);
- issues: cosa non torna nei dati (un campo solo impostato e mai tolto; valori vecchi che sono valori di un altro
  campo, tipico di un'estrazione con righe o colonne mescolate); operazioni senza la loro opposta in una colonna
  attivita' (SET_... senza REMOVE_...);
- mapping_rows: la colonna calcolata CHANGE_KIND e i nomi delle attivita' («Imposta blocco sollecito»,
  «Set Dunning Block»), proposti nel mapping in automatico.
"""
from __future__ import annotations

import re

import pandas as pd

from app.i18n import msg

FIELD_RE = re.compile(r"^(fname|field|field_?name|fieldname|changed_?field|attribute|attribute_?name|campo|nome_?campo)$", re.I)
OLD_RE = re.compile(r"^(value_?old|old_?val(ue)?|oldvalue|prev(ious)?_?val(ue)?|from_?val(ue)?|valore_?(vecchio|precedente))$", re.I)
NEW_RE = re.compile(r"^(value_?new|new_?val(ue)?|newvalue|to_?val(ue)?|valore_?nuovo)$", re.I)
COLUMN = "CHANGE_KIND"

# nomi dei campi piu' comuni nei log di modifiche (italiano, inglese); gli altri restano con il codice
FIELD_LABELS = {
    "MANSP": ("blocco sollecito", "dunning block"), "MANST": ("livello di sollecito", "dunning level"),
    "ZTERM": ("condizioni di pagamento", "payment terms"), "ZLSPR": ("blocco pagamento", "payment block"),
    "ZLSCH": ("metodo di pagamento", "payment method"), "ZFBDT": ("data base della scadenza", "baseline date"),
    "DMBTR": ("importo", "amount"), "WRBTR": ("importo", "amount"), "NETWR": ("valore netto", "net value"),
    "NETPR": ("prezzo netto", "net price"), "MENGE": ("quantità", "quantity"), "EINDT": ("data di consegna", "delivery date"),
    "LIFSK": ("blocco consegna", "delivery block"), "FAKSK": ("blocco fatturazione", "billing block"),
    "ABGRU": ("motivo di rifiuto", "rejection reason"), "SGTXT": ("testo", "text"), "STATUS": ("stato", "status"),
    "PRIORITY": ("priorità", "priority"), "OWNER": ("assegnatario", "owner"), "STAGE": ("fase", "stage"),
}
VERBS = {"it": {"SET": "Imposta", "REMOVE": "Rimuovi", "CHANGE": "Modifica"},
         "en": {"SET": "Set", "REMOVE": "Remove", "CHANGE": "Change"}}
# operazioni che hanno una opposta: se nei dati c'e' solo una delle due, lo si dice
OPPOSITES = [("SET", "REMOVE"), ("SET", "RESET"), ("ADD", "REMOVE"), ("ADD", "DELETE"), ("BLOCK", "UNBLOCK"),
             ("LOCK", "UNLOCK"), ("ASSIGN", "UNASSIGN"), ("IMPOSTA", "RIMUOVI"), ("APERTURA", "CHIUSURA")]


def _clean(v) -> str:
    s = "" if v is None else str(v).strip()
    return "" if s in ("nan", "None") else s


def _kind(o: str, n: str) -> str:
    return "SET" if not o and n else "REMOVE" if o and not n else "CHANGE" if o and n else ""


def detect(df: pd.DataFrame, domains: dict[str, set] | None = None) -> dict | None:
    """{field, old, new, counts: {"SET MANSP": n, ...}, by_field: {campo: {SET, REMOVE, CHANGE}}, foreign: [...]}.
    domains: {NOME COLONNA: valori} delle altre tabelle caricate (es. BSEG.MANSP, BSEG.ZTERM): servono a capire se i
    valori di un campo nel log sono davvero valori di quel campo."""
    cols = list(df.columns)
    field = next((c for c in cols if FIELD_RE.match(c)), None)
    old = next((c for c in cols if OLD_RE.match(c)), None)
    new = next((c for c in cols if NEW_RE.match(c)), None)
    if not (field and old and new) or df.empty:
        return None
    by_field: dict[str, dict[str, int]] = {}
    olds: dict[str, set] = {}
    news: dict[str, set] = {}
    for f, o, n in zip(df[field], df[old], df[new]):
        f, o, n = _clean(f), _clean(o), _clean(n)
        k = _kind(o, n)
        if not f or not k:
            continue
        by_field.setdefault(f, {"SET": 0, "REMOVE": 0, "CHANGE": 0})[k] += 1
        if o:
            olds.setdefault(f, set()).add(o)
        if n:
            news.setdefault(f, set()).add(n)
    # valori di un campo che non sono mai valori di quel campo nelle altre tabelle ma lo sono di un altro campo
    # (es. NT30 come valore vecchio del blocco sollecito: e' una condizione di pagamento)
    foreign = []
    domains = {k.upper(): v for k, v in (domains or {}).items() if v}
    for f in by_field:
        own = domains.get(f.upper())
        if not own:
            continue  # senza il campo in un'altra tabella non c'e' un riferimento: meglio non dire nulla
        for which, values in (("old", olds.get(f, set())), ("new", news.get(f, set()))):
            if not values or values & own:
                continue
            other = next((g for g, dom in domains.items() if g != f.upper() and values <= dom), None)
            if other:
                foreign.append({"field": f, "other": other, "values": sorted(values)[:4], "which": which})
    counts = {f"{k} {f}": n for f, ks in by_field.items() for k, n in ks.items() if n}
    return {"field": field, "old": old, "new": new, "by_field": by_field, "counts": counts, "foreign": foreign}


def label(field: str, lang: str) -> str:
    pair = FIELD_LABELS.get(field.upper())
    if pair:
        return pair[0] if lang == "it" else pair[1]
    return field


def activity_name(value: str, lang: str) -> str:
    """«SET MANSP» -> «Imposta blocco sollecito» / «Set Dunning Block»."""
    kind, _, field = value.partition(" ")
    verb = VERBS.get(lang, VERBS["en"]).get(kind, kind.capitalize())
    name = f"{verb} {label(field, lang)}".strip() if field else verb
    return name if lang == "it" else " ".join(w[:1].upper() + w[1:] for w in name.split())


def summary(log: dict) -> str:
    """«MANSP: 67 impostazioni; ZTERM: 66 modifiche» (testo semplice per il profilo)."""
    parts = []
    for f, ks in log["by_field"].items():
        bits = [f"{n} {k}" for k, n in ks.items() if n]
        parts.append(f"{f}: " + ", ".join(bits))
    return "; ".join(parts[:8])


def issues(table: str, log: dict) -> list[dict]:
    """Avvisi del profilo per un log di modifiche (stessa forma delle issue di profiling)."""
    out = [{"severity": "info", "table": table,
            "title": msg("Log di modifiche: {f} (campo), {o} → {n} (valore vecchio e nuovo)", f=log["field"], o=log["old"], n=log["new"]),
            "impact": msg("L'attività non sta in una colonna: dipende dal campo e da come cambia il valore ({s}). Usare solo "
                          "il campo come attività metterebbe insieme impostazioni, rimozioni e modifiche.", s=summary(log)),
            "action": msg("Nel mapping verrà proposta la colonna calcolata {c}: Imposta / Rimuovi / Modifica + campo, con "
                          "un nome di attività per ogni combinazione presente, da confermare in revisione.", c=COLUMN)}]
    for f, ks in log["by_field"].items():
        if ks["SET"] >= 3 and not ks["REMOVE"] and not ks["CHANGE"]:
            out.append({"severity": "info", "table": table,
                        "title": msg("{f}: solo impostazioni ({n}), nessuna rimozione", f=f, n=ks["SET"]),
                        "impact": msg("Nei dati il campo viene solo impostato e mai tolto: l'analisi non potrà misurare quanto "
                                      "dura (es. un blocco) né quando viene rimosso."),
                        "action": msg("Verifica se le rimozioni sono registrate altrove o se l'estrazione le ha escluse.")})
    for x in log["foreign"]:
        out.append({"severity": "attenzione", "table": table,
                    "title": (msg("{f}: i valori vecchi ({v}) sono valori di {g}", f=x["field"], v=", ".join(x["values"]), g=x["other"])
                              if x["which"] == "old" else
                              msg("{f}: i valori nuovi ({v}) sono valori di {g}", f=x["field"], v=", ".join(x["values"]), g=x["other"])),
                    "impact": msg("Probabile errore di estrazione (righe o colonne mescolate): le modifiche di {f} risultano "
                                  "cambi di valore e non impostazioni o rimozioni, e i valori non sono affidabili.", f=x["field"]),
                    "action": msg("Controlla l'estrazione con chi l'ha preparata e ricarica i dati corretti.")})
    return out


def missing_opposites(table: str, column: str, values: list[str]) -> list[dict]:
    """In una colonna attivita': SET_X senza REMOVE_X (e simili). Detto, perche' l'analisi non lo vedrebbe."""
    up = {v.upper(): v for v in values}
    out = []
    firsts = sorted({a for a, _ in OPPOSITES})
    for a in firsts:
        opposite = [b for x, b in OPPOSITES if x == a]
        b = opposite[0]
        for v_up, v in up.items():
            for sep in ("_", " ", "-"):
                if v_up.startswith(a + sep):
                    rest = v_up[len(a) + 1:]
                    if not any(o.startswith(bb) and o.endswith(rest) for o in up for bb in opposite):
                        out.append({"severity": "info", "table": table,
                                    "title": msg("{c}: c'è «{v}» ma nessuna operazione opposta ({b}…)", c=column, v=v, b=b),
                                    "impact": msg("Nei dati compare solo un verso dell'operazione: l'analisi non potrà vedere "
                                                  "quando viene annullata (es. un blocco tolto)."),
                                    "action": msg("Verifica se l'operazione opposta è registrata in un'altra tabella o se "
                                                  "l'estrazione l'ha esclusa.")})
    seen, unique = set(), []
    for i in out:
        k = str(i["title"])
        if k not in seen:
            seen.add(k)
            unique.append(i)
    return unique
