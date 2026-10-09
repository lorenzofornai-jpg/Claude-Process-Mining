"""Plurale dei nomi dei tipi di oggetto, per frasi corrette («8 fatture», «8 invoices»), non «8 Fattura».

I nomi li sceglie l'utente (o li propone il mapping), in italiano o in inglese, anche in un'interfaccia
dell'altra lingua. Regole:
- italiano: si mette al plurale la prima parola (la testa: «Fattura cliente» -> «Fatture cliente», «Riga
  d'ordine» -> «Righe d'ordine»); le parole straniere (inglesi) e quelle che finiscono in consonante o con
  vocale accentata sono invariabili («100 Purchase Order», «100 ticket»);
- inglese: si mette al plurale l'ultima parola («Purchase Order» -> «Purchase Orders»); un nome italiano
  resta invariabile, come una parola straniera.
Le regole coprono i nomi comuni dei processi aziendali; un nome irregolare resta com'e'.
"""
from __future__ import annotations

import re

# parole inglesi comuni nei processi che in italiano sembrerebbero da volgere al plurale (finiscono in vocale)
_EN_VOWEL = {
    "invoice", "line", "change", "issue", "case", "note", "purchase", "sale", "sales", "price", "service", "schedule",
    "release", "file", "type", "stage", "phase", "code", "profile", "source", "resource", "response", "license",
    "licence", "date", "value", "dispute", "image", "page", "rate", "role", "rule", "time", "site", "store", "title",
    "vehicle", "article", "package", "notice", "balance", "advance", "expense", "maintenance", "insurance", "structure",
    "procedure", "measure", "failure", "closure", "signature", "data", "memo", "video", "audio", "ratio", "portfolio",
    "demo", "logo", "info", "promo", "purchase", "quote", "voice", "case", "update", "office", "storage", "volume",
    "module", "template", "queue", "zone", "price", "place", "scope", "lease", "grade", "trade", "dispatch",
    "customer", "vendor", "supplier", "order", "document", "item", "payment", "receipt", "delivery", "ticket",
    "account", "accounting", "goods", "request", "claim", "contract", "employee", "material", "product", "shipment",
    "credit", "debit", "memo", "entry", "position", "block", "dunning", "clearing", "billing", "open",
}
_EN_INVARIABLE = {"data", "information", "equipment", "stock", "goods", "sales", "series", "staff", "news"}
_IT_ADJ = {"aperta", "aperto", "chiusa", "chiuso", "nuova", "nuovo", "attiva", "attivo", "bloccata", "bloccato"}
_IT_PARTICIPLE = ("ata", "ato", "ita", "ito", "uta", "uto")
_IT_PREP = {"di", "del", "della", "dello", "dei", "delle", "da", "dal", "dalla", "in", "per", "con", "su", "a", "al",
            "alla", "tra", "fra"}
_IT_INVARIABLE = {"re", "gru", "foto", "auto", "moto", "radio", "bici", "euro", "crisi", "analisi", "tesi", "sintesi"}


def _match_case(src: str, out: str) -> str:
    if src.isupper():
        return out.upper()
    if src[:1].isupper():
        return out[:1].upper() + out[1:]
    return out


def _it_word(word: str) -> str:
    w = word.lower()
    if (not w or w in _IT_INVARIABLE or w in _EN_VOWEL or not w[-1] in "aeo"
            or not re.fullmatch(r"[a-zàèéìòù']+", w)):
        return word  # consonante, accentata, straniera o non una parola: invariabile
    if w.endswith(("ca", "ga")):
        out = w[:-1] + "he"
    elif w.endswith(("cia", "gia")):
        out = w[:-3] + (w[-3] + "ie" if w[-4:-3] in "aeiou" else w[-3] + "e")
    elif w.endswith(("ema", "amma", "ista")) and not w.endswith("vista"):
        out = w[:-1] + "i"  # problema -> problemi, programma -> programmi
    elif w.endswith("a"):
        out = w[:-1] + "e"
    elif w.endswith(("co", "go")):
        out = w[:-1] + "hi"
    elif w.endswith("io"):
        out = w[:-1]  # inventario -> inventari
    else:  # -o, -e
        out = w[:-1] + "i"
    return _match_case(word, out)


def _en_word(word: str) -> str:
    w = word.lower()
    if not w or w in _EN_INVARIABLE or not re.fullmatch(r"[a-z]+", w):
        return word
    if w.endswith(("s", "x", "z", "ch", "sh")) and not w.endswith("ss") and w.endswith("s"):
        return word  # gia' plurale (es. «Goods», «Items»)
    if w.endswith(("ss", "x", "z", "ch", "sh")):
        out = w + "es"
    elif w.endswith("y") and w[-2:-1] not in "aeiou":
        out = w[:-1] + "ies"
    else:
        out = w + "s"
    return _match_case(word, out)


def plural(name: str | None, lang: str) -> str:
    """Plurale di un nome di tipo di oggetto nella lingua dell'interfaccia."""
    if not name:
        return name or ""
    parts = name.split(" ")
    if lang == "it":
        out = [_it_word(parts[0])] + parts[1:]
        # aggettivo o participio finale riferito alla testa: «Partita cliente aperta» -> «Partite cliente aperte»
        # (non dopo una preposizione: «Consegna in uscita», «Ordine di partita» restano come sono)
        last = parts[-1].lower()
        if (len(parts) > 1 and (last in _IT_ADJ or last.endswith(_IT_PARTICIPLE))
                and not any(w.lower() in _IT_PREP or w.lower().endswith("'") for w in parts[1:-1])):
            out[-1] = _it_word(parts[-1])
        return " ".join(out)
    if _it_word(parts[0]) != parts[0]:
        return name  # nome italiano in un'interfaccia inglese: invariabile, come le parole straniere
    return " ".join(parts[:-1] + [_en_word(parts[-1])])


def count(n, name: str | None, lang: str) -> str:
    """«1 Fattura», «8 Fatture», «0,9 Clienti» (il plurale vale per tutto cio' che non e' esattamente 1)."""
    from app.i18n import number
    return f"{number(lang, n)} {name if n == 1 else plural(name, lang)}"
