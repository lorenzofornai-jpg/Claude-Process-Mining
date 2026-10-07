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

from dataclasses import replace

from app.connectors.base import TableSchema
from app.i18n import concat, msg
from app.services import catalog, sap_dictionary
from app.services.transformation import default_qualifier, qualifier_for

SAP_TEMPLATE_ID = "sap:standard"

TEMPLATE_LABELS = {
    SAP_TEMPLATE_ID: "dizionario SAP standard",
    catalog.LEARNED_TEMPLATE_ID: "catalogo (dataset già confermato)",
    catalog.TEMPLATE_ID: "modello P2P di esempio",
}


def _rule(col, el, conf, rationale, object_type=None, event_type=None, related=None, qualifier=None,
          activity_values=None):
    return dict(col=col, el=el, object_type=object_type, event_type=event_type,
                related_object_type=related, qualifier=qualifier, conf=conf, rationale=rationale,
                activity_values=activity_values)


def sap_rules(table: TableSchema, code: str, entry: dict) -> list[dict]:
    """Regole dal dizionario SAP, solo per le colonne presenti (con il nome
    esatto della colonna caricata, qualunque sia il maiuscolo/minuscolo)."""
    actual = {c.name.upper(): c.name for c in table.columns}
    with_time = {c.name.upper() for c in table.columns if c.time_column}
    rules: list[dict] = []
    obj = entry["object"]
    if obj:
        obj_type, keys = obj
        for k in sap_dictionary.object_keys(keys, set(actual)):
            rules.append(_rule(actual[k], "object_type.key", 0.95,
                               msg("Dizionario SAP: {c} è chiave di {o} ({l}).", c=f"{code}.{k}", o=obj_type, l=entry["label"]),
                               object_type=obj_type))
        for col, descr in entry["object_attributes"].items():
            if col in actual:
                rules.append(_rule(actual[col], "object_type.attribute", 0.9,
                                   msg("Dizionario SAP: {c} = {d}.", c=f"{code}.{col}", d=descr), object_type=obj_type))
    for evt, date_cols, evt_attrs in entry["events"]:
        # a parita' di significato si preferisce la data che ha anche l'ora (es. CPUDT + CPUTM
        # invece di BUDAT, che in SAP e' solo un giorno): gli eventi dello stesso giorno si ordinano
        present = [d for d in date_cols if d in actual]
        date_col = next((d for d in present if d in with_time), present[0] if present else None)
        if date_col is None:
            continue
        note = msg("Preferita a {d} perché ha anche l'ora.", d=present[0]) if date_col != present[0] else None
        rules.append(_rule(actual[date_col], "event_type.timestamp", 0.92,
                           concat(msg("Dizionario SAP: {c} è la data dell'attività «{e}».", c=f"{code}.{date_col}", e=evt), note),
                           event_type=evt))
        for col, descr in evt_attrs.items():
            if col in actual:
                rules.append(_rule(actual[col], "event_type.attribute", 0.88,
                                   msg("Dizionario SAP: {c} = {d}.", c=f"{code}.{col}", d=descr), event_type=evt))
    activity = entry.get("activity")
    if activity and activity["column"] in actual and any(
        r["el"] == "event_type.timestamp" and r["event_type"] == activity["event"] for r in rules
    ):
        codes = ", ".join(f"{k} {v}" for k, v in activity["values"].items())
        rules.append(_rule(actual[activity["column"]], "event_type.activity", 0.92,
                           msg("Dizionario SAP: {c} dice quale operazione è registrata in ogni riga ({v}): ogni codice è "
                               "un'attività distinta.", c=f"{code}.{activity['column']}", v=codes),
                           event_type=activity["event"], activity_values=dict(activity["values"])))
    for evt, related, qualifier in entry["relations"]:
        rules.append(_rule(None, "e2o_relationship", 0.9,
                           msg("Dizionario SAP: l'attività «{e}» riguarda {r}.", e=evt, r=related),
                           event_type=evt, related=related, qualifier=qualifier))
    for evt, related, join_col, qualifier in entry["joins"]:
        if join_col in actual:
            rules.append(_rule(actual[join_col], "e2o_relationship", 0.9,
                               msg("Dizionario SAP: {t} collega l'attività «{e}» a {r} tramite {j}.", t=code, e=evt, r=related,
                                   j=join_col),
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


def _with_time_columns(proposals: list, tables: list[TableSchema]) -> list:
    """Data e ora in colonne separate (CPUDT + CPUTM, created_date + created_time):
    una colonna di sole ore non e' mai la data di un evento ne' un attributo utile;
    se e' abbinata alla data di un evento diventa la sua ora (event_type.time)."""
    time_cols = {(t.name, c.name) for t in tables for c in t.columns if c.inferred_type == "time"}
    time_of = {(t.name, c.name): c.time_column for t in tables for c in t.columns if c.time_column}
    if not time_cols:
        return proposals
    has_time = {(p.source_table, p.event_type) for p in proposals if p.ocel_element == "event_type.time"}
    out = []
    added = []
    for p in proposals:
        key = (p.source_table, p.source_column)
        if key in time_cols and p.ocel_element in ("event_type.timestamp", "object_type.attribute",
                                                   "event_type.attribute"):
            continue
        out.append(p)
        tc = time_of.get(key)
        if p.ocel_element == "event_type.timestamp" and tc and (p.source_table, p.event_type) not in has_time:
            has_time.add((p.source_table, p.event_type))
            added.append(replace(
                p, source_column=tc, ocel_element="event_type.time", object_type=None, attribute_name=None,
                qualifier=None, related_object_type=None, confidence=0.9, activity_values=None,
                rationale=msg("{t} contiene l'ora di {d}: unite danno l'istante esatto dell'evento, così gli eventi "
                              "dello stesso giorno si ordinano e i tempi si misurano al minuto.", t=tc, d=p.source_column),
            ))
    return out + added


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
    proposals = _with_time_columns(proposals, tables)
    planned = {(t.name, c.name): c.planned_reason for t in tables for c in t.columns
               if getattr(c, "planned_reason", None)}
    for p in proposals:
        reason = planned.get((p.source_table, p.source_column))
        if p.ocel_element == "event_type.timestamp" and reason:
            # resta una proposta, ma incerta: non si accetta in blocco e la revisione la apre
            p.confidence = min(p.confidence, 0.5)
            p.rationale = concat(msg("Attenzione: probabile data prevista o di scadenza ({r}). Come evento metterebbe "
                                     "nel processo un passo non avvenuto: meglio rifiutarla o trasformarla in attributo.",
                                     r=reason), p.rationale)
    for p in proposals:
        if p.ocel_element == "e2o_relationship" and not (p.qualifier or "").strip():
            # a volte l'AI scrive il qualifier nella motivazione ("q: for customer") invece che nel campo
            p.qualifier = qualifier_for(p.related_object_type, p.rationale)

    # codici della colonna attivita' gia' coperti da un'altra tabella del dataset: esclusi
    for p in proposals:
        if p.ocel_element != "event_type.activity" or p.based_on_template != SAP_TEMPLATE_ID or not p.activity_values:
            continue
        found = sap_dictionary.lookup(p.source_table, list(columns.get(p.source_table, ())))
        covered = (found[1].get("activity") or {}).get("covered_by", {}) if found else {}
        for value, other_event in covered.items():
            if other_event in event_table and p.activity_values.get(value):
                p.activity_values = {**p.activity_values, value: ""}
                p.rationale = concat(p.rationale, msg("Il codice {v} è escluso: lo stesso fatto arriva già da «{e}» ({t}).",
                                                      v=value, e=other_event, t=event_table[other_event]))

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
    return kept + _header_item_links(kept, tables)


def _header_item_links(proposals: list, tables: list[TableSchema]) -> list:
    """Posizioni senza eventi (es. BSEG, EKPO, VBAP, righe ordine): se la loro chiave
    contiene la chiave di un oggetto definito da un'altra tabella che ha eventi (la
    testata: BKPF, EKKO, VBAK...), gli eventi della testata riguardano anche le sue
    posizioni. Si propone il collegamento "ponte": per ogni evento della testata si
    cercano nella tabella delle posizioni le righe con lo stesso valore della colonna
    in comune piu' selettiva. Vale per qualunque sistema, non solo SAP."""
    keys: dict[str, list[str]] = {}
    table_of: dict[str, str] = {}
    sample: dict[str, object] = {}
    for p in proposals:
        if p.ocel_element == "object_type.key" and p.object_type:
            keys.setdefault(p.object_type, []).append(p.source_column)
            table_of[p.object_type] = p.source_table
            sample.setdefault(p.object_type, p)
    events_of: dict[str, list[str]] = {}
    for p in proposals:
        if p.ocel_element == "event_type.timestamp" and p.event_type:
            events_of.setdefault(p.source_table, [])
            if p.event_type not in events_of[p.source_table]:
                events_of[p.source_table].append(p.event_type)
    linked = {p.related_object_type for p in proposals if p.ocel_element == "e2o_relationship"}
    columns = {t.name: {c.name.upper(): c for c in t.columns} for t in tables}

    added = []
    for obj, obj_keys in keys.items():
        table = table_of[obj]
        if table in events_of or obj in linked or len(obj_keys) < 2:
            continue  # ha gia' eventi suoi o collegati
        own = {k.upper() for k in obj_keys}
        best = None
        for header, header_keys in keys.items():
            h_table = table_of[header]
            h_own = {k.upper() for k in header_keys}
            if h_table == table or h_table not in events_of or not (h_own < own):
                continue
            if not h_own <= set(columns.get(h_table, {})) or not h_own <= set(columns.get(table, {})):
                continue
            if best is None or len(h_own) > len(best[1]):
                best = (header, h_own, h_table)
        if best is None:
            continue
        header, h_own, h_table = best
        # la colonna in comune piu' selettiva (es. il numero documento, non la societa')
        join = max(h_own, key=lambda k: getattr(columns[h_table][k], "distinct_ratio", 0))
        join_col = columns[table][join].name
        for event in events_of[h_table]:
            added.append(replace(
                sample[obj], source_column=join_col, ocel_element="e2o_relationship", object_type=None,
                event_type=event, attribute_name=None, related_object_type=obj, qualifier=default_qualifier(obj),
                confidence=0.86, activity_values=None, based_on_template=None,
                rationale=msg("Le righe di {t} sono le posizioni di {h} ({ht}): gli eventi «{e}» riguardano anche le "
                              "sue posizioni, collegate tramite {c}. Senza questo collegamento le posizioni resterebbero "
                              "senza eventi.", t=table, h=header, ht=h_table, e=event, c=join_col),
            ))
    return added
