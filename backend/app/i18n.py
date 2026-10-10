"""Lingua dell'interfaccia: italiano (predefinito) e inglese.

- Il testo di riferimento e' l'italiano: e' anche la chiave del catalogo
  inglese (app/i18n_en.py). Un testo senza traduzione resta in italiano.
- La lingua si sceglie in qualsiasi momento dal selettore IT | EN in alto
  (GET /lang/{codice}); vale subito, resta in sessione e sull'utente
  (User.language), cosi' torna uguale al prossimo accesso.
- Nei template: {{ _("Testo con {nome}", nome=valore) }}.
- I messaggi prodotti dai servizi (profilo dei dati, controlli di qualita',
  motivazioni del mapping...) sono "messaggi strutturati" {"m": testo, "p":
  parametri}, tradotti solo quando si mostrano: cambiando lingua cambiano
  anche i risultati gia' calcolati. Nel database si salvano come JSON
  (to_text); il filtro |tr li riconosce, come i testi semplici di prima.
"""
from __future__ import annotations

import json

from markupsafe import Markup, escape

LANGS = {"it": "Italiano", "en": "English"}
DEFAULT_LANG = "it"


def _catalog() -> dict[str, str]:
    from app.i18n_en import EN
    return EN


def get_lang(request) -> str:
    """Lingua della richiesta: scelta dell'utente (sessione), altrimenti quella del browser."""
    try:
        lang = request.session.get("lang")
    except (AssertionError, AttributeError):
        lang = None
    if lang in LANGS:
        return lang
    accept = (request.headers.get("accept-language") or "").lower() if hasattr(request, "headers") else ""
    return "en" if accept.startswith("en") else DEFAULT_LANG


def msg(text: str, **params) -> dict:
    """Messaggio da tradurre al momento di mostrarlo."""
    return {"m": text, "p": params} if params else {"m": text}


def concat(*parts) -> dict:
    """Frasi una dopo l'altra (es. una motivazione con un'avvertenza aggiunta)."""
    return {"seq": [p for p in parts if p]}


def joined(items, sep: str = ", ") -> dict:
    """Elenco di testi o messaggi uniti da un separatore."""
    return {"list": list(items), "sep": sep}


def t(lang: str, text: str, **params) -> str:
    out = _catalog().get(text, text) if lang == "en" else text
    if params:
        values = {k: render(lang, v) for k, v in params.items()}
        try:
            out = out.format(**values)
        except (KeyError, IndexError, ValueError):
            out = text.format(**values)
    return out


def ui_labels(lang: str, labels: dict[str, str]) -> dict[str, str]:
    """Etichette dell'interfaccia nella lingua dell'utente, da passare a Claude: {chiave: testo come lo vede}.
    I testi italiani di `labels` sono gia' nel catalogo (sono quelli dei template)."""
    return {key: t(lang, text) for key, text in labels.items()}


def plural(name: str) -> dict:
    """Nome di un tipo di oggetto da mostrare al plurale nella lingua di chi legge («fatture», «invoices»)."""
    return {"plural": name}


def count(n, name: str) -> dict:
    """Numero e nome di un tipo di oggetto, accordati: «1 Fattura», «8 Fatture», «8 Invoices»."""
    return {"count": n, "noun": name}


def number(lang: str, n) -> str:
    """Numero con il separatore decimale della lingua (interi senza decimali)."""
    if isinstance(n, float) and not n.is_integer():
        text = f"{n:.1f}"
        return text.replace(".", ",") if lang == "it" else text
    return str(int(n)) if isinstance(n, float) else str(n)


def render(lang: str, value) -> str:
    """Testo da mostrare per un messaggio strutturato, una lista di messaggi o un testo."""
    if value is None:
        return ""
    if isinstance(value, dict) and "plural" in value:
        from app.services.nouns import plural as _plural
        return _plural(value["plural"], lang)
    if isinstance(value, dict) and "noun" in value:
        from app.services.nouns import count as _count
        return _count(value.get("count"), value["noun"], lang)
    if isinstance(value, dict) and "m" in value:
        return t(lang, value["m"], **(value.get("p") or {}))
    if isinstance(value, dict) and "seq" in value:
        return " ".join(render(lang, v) for v in value["seq"])
    if isinstance(value, dict) and "list" in value:
        return value.get("sep", ", ").join(render(lang, v) for v in value["list"])
    if isinstance(value, (list, tuple)):
        return "; ".join(render(lang, v) for v in value)
    if isinstance(value, str) and value.startswith(('{"m"', '{"seq"')):
        try:
            return render(lang, json.loads(value))
        except ValueError:
            return value
    if isinstance(value, str):
        return t(lang, value) if lang == "en" else value
    return str(value)


def to_text(value) -> str:
    """Per salvare un messaggio strutturato in una colonna di testo del database."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value or ""


def utc_iso(dt) -> str:
    """Data e ora salvate (UTC, anche senza fuso indicato) in ISO con «Z»: il browser le converte nell'ora locale."""
    from datetime import timezone
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def localtime(dt) -> Markup:
    """<time> con l'ora UTC: lo script di base.html la mostra nel fuso del dispositivo dell'utente (senza
    JavaScript resta l'ora UTC, dichiarata come tale)."""
    if dt is None:
        return Markup("—")
    iso = utc_iso(dt)
    return Markup('<time class="lt" datetime="{}">{} UTC</time>').format(iso, iso[:16].replace("T", " "))


def setup_templates(templates) -> None:
    """Rende disponibili _(), |tr e la lingua corrente in tutti i template."""
    from jinja2 import pass_context

    @pass_context
    def gettext(ctx, text: str, **params):
        request = ctx.get("request")
        lang = get_lang(request) if request is not None else DEFAULT_LANG
        # i parametri (nomi di processi, colonne...) sono dati: si fa l'escape, il testo del catalogo no
        safe = {k: (v if isinstance(v, Markup) else escape(render(lang, v))) for k, v in params.items()}
        return Markup(t(lang, text, **safe))

    @pass_context
    def translate(ctx, value):
        request = ctx.get("request")
        return render(get_lang(request) if request is not None else DEFAULT_LANG, value)

    @pass_context
    def current_lang(ctx):
        request = ctx.get("request")
        return get_lang(request) if request is not None else DEFAULT_LANG

    def strong(value) -> Markup:
        """Valore in grassetto dentro una frase tradotta: _("Processo: {p}", p=strong(nome))."""
        return Markup("<strong>{}</strong>").format(value)

    @pass_context
    def money(ctx, value) -> str:
        """Importo con due decimali: virgola in italiano, punto in inglese."""
        text = "%.2f" % (value or 0)
        return text.replace(".", ",") if current_lang(ctx) == "it" else text

    templates.env.globals["money"] = money
    templates.env.globals["strong"] = strong
    templates.env.globals["_"] = gettext
    templates.env.globals["current_lang"] = current_lang
    templates.env.globals["languages"] = LANGS
    templates.env.filters["tr"] = translate
    templates.env.filters["localtime"] = localtime
