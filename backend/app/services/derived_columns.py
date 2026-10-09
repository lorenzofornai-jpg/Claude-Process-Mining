"""Colonne calcolate proposte nel mapping a partire dal profilo dei dati (fase 1), qualunque mapper abbia lavorato.

- Log di modifiche (campo, valore vecchio, nuovo): colonna CHANGE_KIND = Imposta / Rimuovi / Modifica + campo, usata
  come colonna attivita' degli eventi della tabella, con un nome per ogni combinazione presente («Set Dunning
  Block», «Remove Dunning Block», «Change Payment Terms»).
- Chiave dentro un valore composto (es. SAP CDHDR.OBJECTID = societa' + documento + esercizio): colonna con quella
  parte del valore, usata per collegare gli eventi della tabella all'oggetto di quella chiave.
"""
from __future__ import annotations

from app.i18n import msg
from app.services import change_logs
from app.services.ai_mapping import MappingProposal
from app.services.transformation import COMPUTED


def _rule(table, col, el, rationale, **kw) -> MappingProposal:
    base = dict(source_table=table, source_column=col, ocel_element=el, object_type=None, event_type=None,
                attribute_name=None, qualifier=None, related_object_type=None, confidence=0.95,
                rationale=rationale, based_on_template=None, activity_values=None)
    base.update(kw)
    return MappingProposal(**base)


def change_log_rows(proposals: list[MappingProposal], table: str, log: dict, lang: str,
                    column: str = change_logs.COLUMN) -> list[MappingProposal]:
    """Colonna calcolata e colonna attivita' per un log di modifiche (modifica le proposte di attivita' gia' fatte
    su quella tabella: il solo campo come attivita' mescolerebbe impostazioni, rimozioni e modifiche)."""
    if any(p.ocel_element == COMPUTED and p.source_table == table and p.source_column == column for p in proposals):
        return []
    spec = {"rule": "change", "field": log["field"], "old": log["old"], "new": log["new"]}
    names = {v: change_logs.activity_name(v, lang) for v in log.get("values") or log.get("counts") or []}
    why = msg("Log di modifiche: l'attività dipende dal campo ({f}) e da come cambia il valore ({o} → {n}).",
              f=log["field"], o=log["old"], n=log["new"])
    out = [_rule(table, column, COMPUTED, why, activity_values=spec)]
    events = [p.event_type for p in proposals if p.source_table == table and p.ocel_element == "event_type.timestamp" and p.event_type]
    for event in dict.fromkeys(events):
        existing = [p for p in proposals if p.source_table == table and p.ocel_element == "event_type.activity" and p.event_type == event]
        if existing:
            for p in existing:
                p.source_column, p.activity_values, p.rationale, p.confidence = column, names, why, 0.95
        else:
            out.append(_rule(table, column, "event_type.activity", why, event_type=event, activity_values=names))
    return out


def embedded_key_rows(proposals: list[MappingProposal], emb: dict) -> list[MappingProposal]:
    """Colonna con la parte di un valore composto che e' la chiave di un'altra tabella, e collegamenti degli eventi
    della tabella all'oggetto di quella chiave."""
    table, parent, pcol = emb["table"], emb["parent_table"], emb["parent_column"]
    column = f"{emb['column']}_{pcol}"
    if any(p.ocel_element == COMPUTED and p.source_table == table and p.source_column == column for p in proposals):
        return []
    obj = next((p.object_type for p in proposals if p.ocel_element == "object_type.key" and p.source_table == parent
                and p.source_column == pcol and p.object_type), None)
    events = [p.event_type for p in proposals if p.source_table == table and p.ocel_element == "event_type.timestamp" and p.event_type]
    if not obj or not events:
        return []
    a = int(emb["start"]) + 1
    why = msg("{c} contiene in posizione {a}–{b} il valore di {k} ({p}): collega gli eventi a {o}.", c=f"{table}.{emb['column']}",
              a=a, b=a + int(emb["length"]) - 1, k=pcol, p=parent, o=obj)
    out = [_rule(table, column, COMPUTED, why,
                 activity_values={"rule": "slice", "column": emb["column"], "start": str(emb["start"]), "length": str(emb["length"])})]
    for event in dict.fromkeys(events):
        if any(p.ocel_element == "e2o_relationship" and p.event_type == event and p.related_object_type == obj for p in proposals):
            continue
        out.append(_rule(table, column, "e2o_relationship", why, event_type=event, related_object_type=obj,
                         qualifier=f"for {obj.lower()}"))
    return out


def apply(proposals: list[MappingProposal], data_profile: dict, lang: str) -> list[MappingProposal]:
    """Le proposte con le colonne calcolate suggerite dal profilo dei dati."""
    out = list(proposals)
    for table, log in (data_profile.get("change_logs") or {}).items():
        out.extend(change_log_rows(out, table, log, lang))
    for emb in data_profile.get("embedded_keys") or []:
        out.extend(embedded_key_rows(out, emb))
    return out
