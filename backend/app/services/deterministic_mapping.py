"""Mapping deterministico: tutto cio' che si riconosce con certezza senza AI.

Fonti, in ordine di priorita':
1. catalogo appreso: mapping gia' confermati da un utente in un dataset
   precedente per una tabella con lo stesso nome e colonne compatibili;
2. modello P2P di esempio (tabelle sintetiche del prototipo);
3. dizionario delle tabelle SAP standard (services/sap_dictionary.py).

Le tabelle riconosciute qui non vanno a Claude: costo zero e risultato
immediato. Le altre restano all'AI, che riceve il modello gia' definito per
usare gli stessi nomi di oggetti ed eventi.
"""
from __future__ import annotations

from app.connectors.base import TableSchema
from app.services import catalog, sap_dictionary

SAP_TEMPLATE_ID = "sap:standard"

TEMPLATE_LABELS = {
    SAP_TEMPLATE_ID: "dizionario SAP standard",
    catalog.LEARNED_TEMPLATE_ID: "catalogo (dataset già confermato)",
    catalog.TEMPLATE_ID: "modello P2P di esempio",
}


def _rule(col, el, conf, rationale, object_type=None, event_type=None, related=None, qualifier=None):
    return dict(col=col, el=el, object_type=object_type, event_type=event_type,
                related_object_type=related, qualifier=qualifier, conf=conf, rationale=rationale)


def sap_rules(table: TableSchema, code: str, entry: dict) -> list[dict]:
    """Regole dal dizionario SAP, solo per le colonne presenti (con il nome
    esatto della colonna caricata, qualunque sia il maiuscolo/minuscolo)."""
    actual = {c.name.upper(): c.name for c in table.columns}
    rules: list[dict] = []
    obj = entry["object"]
    if obj:
        obj_type, keys = obj
        for k in sap_dictionary.object_keys(keys, set(actual)):
            rules.append(_rule(actual[k], "object_type.key", 0.95,
                               f"Dizionario SAP: {code}.{k} è chiave di {obj_type} ({entry['label']}).",
                               object_type=obj_type))
        for col, descr in entry["object_attributes"].items():
            if col in actual:
                rules.append(_rule(actual[col], "object_type.attribute", 0.9,
                                   f"Dizionario SAP: {code}.{col} = {descr}.", object_type=obj_type))
    for evt, date_cols, evt_attrs in entry["events"]:
        date_col = next((d for d in date_cols if d in actual), None)
        if date_col is None:
            continue
        rules.append(_rule(actual[date_col], "event_type.timestamp", 0.92,
                           f"Dizionario SAP: {code}.{date_col} è la data dell'attività «{evt}».", event_type=evt))
        for col, descr in evt_attrs.items():
            if col in actual:
                rules.append(_rule(actual[col], "event_type.attribute", 0.88,
                                   f"Dizionario SAP: {code}.{col} = {descr}.", event_type=evt))
    for evt, related, qualifier in entry["relations"]:
        rules.append(_rule(None, "e2o_relationship", 0.9,
                           f"Dizionario SAP: l'attività «{evt}» riguarda {related}.",
                           event_type=evt, related=related, qualifier=qualifier))
    for evt, related, join_col, qualifier in entry["joins"]:
        if join_col in actual:
            rules.append(_rule(actual[join_col], "e2o_relationship", 0.9,
                               f"Dizionario SAP: {code} collega l'attività «{evt}» a {related} tramite {join_col}.",
                               event_type=evt, related=related, qualifier=qualifier))
    return rules


def rules_for(table: TableSchema, builtin_templates: dict[str, list[dict]]) -> tuple[list[dict], str] | None:
    """Regole deterministiche per una tabella, con l'origine; None se la
    tabella non e' riconosciuta e va lasciata all'AI."""
    columns = [c.name for c in table.columns]
    dynamic = catalog.dynamic_lookup(table.name, columns)
    if dynamic:
        return dynamic[1], catalog.LEARNED_TEMPLATE_ID
    if catalog.lookup(table.name) and table.name in builtin_templates:
        return builtin_templates[table.name], catalog.TEMPLATE_ID
    found = sap_dictionary.lookup(table.name, columns)
    if found:
        rules = sap_rules(table, *found)
        if rules:
            return rules, SAP_TEMPLATE_ID
    return None


def finalize(proposals: list, tables: list[TableSchema]) -> list:
    """Toglie i collegamenti del dizionario SAP che nel dataset caricato non
    reggono: oggetto collegato assente, evento assente, oppure (collegamento
    diretto) chiave dell'oggetto non presente nella tabella dell'evento. Per i
    collegamenti diretti assegna come colonna sorgente la prima colonna chiave."""
    objects: dict[str, list[str]] = {}
    for p in proposals:
        if p.ocel_element == "object_type.key" and p.object_type:
            objects.setdefault(p.object_type, []).append(p.source_column)
    event_table = {p.event_type: p.source_table for p in proposals if p.ocel_element == "event_type.timestamp"}
    columns = {t.name: {c.name for c in t.columns} for t in tables}

    kept = []
    for p in proposals:
        if p.ocel_element != "e2o_relationship" or p.based_on_template != SAP_TEMPLATE_ID:
            kept.append(p)
            continue
        keys = objects.get(p.related_object_type)
        if not keys or p.event_type not in event_table:
            continue
        if p.source_column is None:  # collegamento diretto
            if not set(keys) <= columns.get(p.source_table, set()):
                continue
            p.source_column = sorted(keys)[0]
        kept.append(p)
    return kept
