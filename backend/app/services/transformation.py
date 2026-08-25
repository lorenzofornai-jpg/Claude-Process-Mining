"""Transformation Engine: da field_mapping confermato a log OCEL 2.0.

Riceve i dati grezzi delle tabelle sorgente (dict per riga, come restituiti
dal Connector) e la lista di mapping CONFERMATI (non piu' semplici proposte),
e produce un documento OCEL 2.0 JSON standard: objectTypes, eventTypes,
objects, events con relazioni event-to-object (E2O).

Semplificazioni deliberate per questo prototipo (documentate anche nel
README): gli attributi oggetto non sono time-varying (nessuna storia dei
cambiamenti, solo snapshot), i valori attributo restano stringhe, le
relazioni object-to-object non sono modellate (si usano solo E2O, che sono
cio' che serve per la process discovery multi-oggetto).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ObjectTypeDefCompiled:
    name: str
    source_table: str
    key_columns: list[str]
    attribute_columns: list[str] = field(default_factory=list)


@dataclass
class EventTypeDefCompiled:
    name: str
    source_table: str
    timestamp_column: str
    attribute_columns: list[str] = field(default_factory=list)


@dataclass
class SkipRecord:
    event_type: str
    source_table: str
    reason: str
    row_preview: dict


def compile_defs(confirmed: list[dict]) -> tuple[dict[str, ObjectTypeDefCompiled], dict[str, EventTypeDefCompiled]]:
    object_defs: dict[str, ObjectTypeDefCompiled] = {}
    event_defs: dict[str, EventTypeDefCompiled] = {}

    for m in confirmed:
        if m["ocel_element"] == "object_type.key":
            od = object_defs.setdefault(
                m["object_type"],
                ObjectTypeDefCompiled(name=m["object_type"], source_table=m["source_table"], key_columns=[]),
            )
            if m["source_column"] not in od.key_columns:
                od.key_columns.append(m["source_column"])
        elif m["ocel_element"] == "event_type.timestamp":
            event_defs[m["event_type"]] = EventTypeDefCompiled(
                name=m["event_type"], source_table=m["source_table"], timestamp_column=m["source_column"]
            )

    for od in object_defs.values():
        od.key_columns.sort()

    for m in confirmed:
        if m["ocel_element"] == "object_type.attribute" and m["object_type"] in object_defs:
            object_defs[m["object_type"]].attribute_columns.append(m["source_column"])
        elif m["ocel_element"] == "event_type.attribute" and m["event_type"] in event_defs:
            event_defs[m["event_type"]].attribute_columns.append(m["source_column"])

    return object_defs, event_defs


def _build_object_id(obj_def: ObjectTypeDefCompiled, row: dict) -> str | None:
    values = [str(row.get(c, "")).strip() for c in obj_def.key_columns]
    if not obj_def.key_columns or any(v == "" or v == "nan" for v in values):
        return None
    return f"{obj_def.name}:" + "|".join(values)


def _parse_time(raw: str | None) -> datetime | None:
    if not raw or str(raw).strip() in ("", "nan"):
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(raw).strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _resolve_related_objects(
    event_row: dict,
    mapping: dict,
    object_defs: dict[str, ObjectTypeDefCompiled],
    tables_data: dict[str, list[dict]],
) -> list[str]:
    target_def = object_defs.get(mapping["related_object_type"])
    if target_def is None:
        return []

    if all(c in event_row for c in target_def.key_columns):
        obj_id = _build_object_id(target_def, event_row)
        return [obj_id] if obj_id else []

    join_table = mapping["source_table"]
    join_col = mapping["source_column"]
    joined_rows = [r for r in tables_data.get(join_table, []) if r.get(join_col) == event_row.get(join_col)]
    ids = [_build_object_id(target_def, r) for r in joined_rows]
    return [i for i in ids if i]


def build_ocel(
    tables_data: dict[str, list[dict]],
    confirmed: list[dict],
) -> tuple[dict, list[SkipRecord], dict]:
    object_defs, event_defs = compile_defs(confirmed)

    relationship_rules = [m for m in confirmed if m["ocel_element"] == "e2o_relationship"]

    objects: dict[str, dict] = {}
    for obj_def in object_defs.values():
        for row in tables_data.get(obj_def.source_table, []):
            obj_id = _build_object_id(obj_def, row)
            if obj_id is None or obj_id in objects:
                continue
            attrs = []
            for col in obj_def.attribute_columns:
                val = row.get(col)
                if val is not None and str(val).strip() not in ("", "nan"):
                    attrs.append({"name": col, "time": "1970-01-01T00:00:00Z", "value": str(val)})
            objects[obj_id] = {"id": obj_id, "type": obj_def.name, "attributes": attrs}

    events: list[dict] = []
    skip_log: list[SkipRecord] = []
    event_counter = 0

    for evt_def in event_defs.values():
        home_object_def = next((o for o in object_defs.values() if o.source_table == evt_def.source_table), None)
        own_rules = [m for m in relationship_rules if m["event_type"] == evt_def.name]

        for row in tables_data.get(evt_def.source_table, []):
            ts = _parse_time(row.get(evt_def.timestamp_column))
            if ts is None:
                skip_log.append(SkipRecord(
                    event_type=evt_def.name, source_table=evt_def.source_table,
                    reason=f"timestamp mancante o non parsabile in colonna '{evt_def.timestamp_column}'",
                    row_preview={k: row.get(k) for k in list(row)[:4]},
                ))
                continue

            event_counter += 1
            event_id = f"e{event_counter}"

            relationships = []
            if home_object_def is not None:
                home_id = _build_object_id(home_object_def, row)
                if home_id:
                    relationships.append({"objectId": home_id, "qualifier": "involves"})

            for rule in own_rules:
                for target_id in _resolve_related_objects(row, rule, object_defs, tables_data):
                    relationships.append({"objectId": target_id, "qualifier": rule["qualifier"]})

            attrs = []
            for col in evt_def.attribute_columns:
                val = row.get(col)
                if val is not None and str(val).strip() not in ("", "nan"):
                    attrs.append({"name": col, "value": str(val)})

            events.append({
                "id": event_id,
                "type": evt_def.name,
                "time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "attributes": attrs,
                "relationships": relationships,
            })

    events.sort(key=lambda e: e["time"])

    ocel = {
        "objectTypes": [
            {"name": od.name, "attributes": [{"name": c, "type": "string"} for c in od.attribute_columns]}
            for od in object_defs.values()
        ],
        "eventTypes": [
            {"name": ed.name, "attributes": [{"name": c, "type": "string"} for c in ed.attribute_columns]}
            for ed in event_defs.values()
        ],
        "objects": list(objects.values()),
        "events": events,
    }

    stats = {
        "object_count": len(objects),
        "event_count": len(events),
        "object_types": len(object_defs),
        "event_types": len(event_defs),
        "skipped_count": len(skip_log),
    }
    return ocel, skip_log, stats
