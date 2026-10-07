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

Eventi da colonna "attivita'": un tipo di evento del mapping (event_type)
puo' avere, oltre alla data, una colonna che dice cosa e' successo in ogni
riga (tipo movimento, azione, stato, causale; in SAP EKBE.VGABE). Allora
l'event_type del mapping e' solo il gruppo che porta data, collegamenti e
attributi, e l'attivita' di ogni evento si legge dal valore della colonna:
- activity_values = {valore: nome attivita'}: traduzione dei codici;
  un valore tradotto con "" e' escluso deliberatamente (tracciato negli
  scarti); un valore non presente nella tabella resta, con il nome
  "<gruppo> [<colonna>=<valore>]", cosi' nessun evento sparisce in silenzio;
- activity_values = None: il valore e' gia' il nome dell'attivita' (export
  gia' in forma di event log: caso, attivita', data).

Data e ora in colonne separate (SAP CPUDT + CPUTM, created_date +
created_time): l'elemento event_type.time indica la colonna con l'ora, che
si unisce alla data dell'evento. Se l'ora di una riga manca o non e'
leggibile l'evento resta, con la sola data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.services.timeparts import parse_time_of_day


@dataclass
class ObjectTypeDefCompiled:
    name: str
    source_table: str
    key_columns: list[str]
    attribute_columns: list[str] = field(default_factory=list)
    # divisione per valore (object_type.split): {valore della colonna: tipo di oggetto} ("" = escluso)
    split_column: str | None = None
    split_values: dict[str, str] | None = None

    def type_for(self, row: dict) -> tuple[str | None, str]:
        """(tipo di oggetto, esito) per una riga: senza divisione e' sempre il tipo del mapping; con la
        divisione e' il tipo indicato per il valore della colonna, None se il valore e' escluso.
        Un valore non previsto (o vuoto) resta nel tipo del mapping, cosi' nessun oggetto sparisce in silenzio."""
        if not self.split_column:
            return self.name, "ok"
        value = _clean(row.get(self.split_column))
        if (self.split_values or {}).get(value) is not None:
            name = self.split_values[value].strip()
            return (name, "ok") if name else (None, "excluded")
        return self.name, "unmapped"

    def output_types(self) -> list[str]:
        """Tipi di oggetto che questa definizione puo' produrre."""
        if not self.split_column:
            return [self.name]
        out = [n.strip() for n in (self.split_values or {}).values() if n and n.strip()]
        return list(dict.fromkeys(out + [self.name]))


@dataclass
class EventTypeDefCompiled:
    name: str
    source_table: str
    timestamp_column: str
    attribute_columns: list[str] = field(default_factory=list)
    time_column: str | None = None
    activity_column: str | None = None
    activity_values: dict[str, str] | None = None

    def activity_for(self, row: dict) -> tuple[str | None, str]:
        """(nome attivita', esito) per una riga: esito e' "ok", "unmapped"
        (valore senza traduzione, tenuto con il codice), "excluded" o "missing"."""
        if not self.activity_column:
            return self.name, "ok"
        value = _clean(row.get(self.activity_column))
        if value == "":
            return None, "missing"
        if self.activity_values is None:
            return value, "ok"
        if value in self.activity_values:
            name = (self.activity_values[value] or "").strip()
            return (name, "ok") if name else (None, "excluded")
        return f"{self.name} [{self.activity_column}={value}]", "unmapped"


@dataclass
class SkipRecord:
    event_type: str
    source_table: str
    reason: str
    row_preview: dict
    # "timestamp": data mancante/illeggibile (problema di qualita');
    # "excluded": valore della colonna attivita' escluso deliberatamente nel mapping;
    # "excluded_object": l'oggetto della riga e' escluso dalla divisione per valore (es. documenti che non
    # sono ne' fatture ne' incassi), quindi anche il suo evento;
    # "activity": colonna attivita' vuota
    kind: str = "timestamp"


def _clean(raw) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    return "" if s == "nan" else s


# campi del mapping pertinenti per ogni tipo di riga (gli altri restano vuoti)
FIELDS_BY_ELEMENT = {
    "object_type.key": ["object_type"],
    "object_type.attribute": ["object_type", "attribute_name"],
    "event_type.timestamp": ["event_type"],
    "event_type.time": ["event_type"],
    "event_type.activity": ["event_type", "activity_values"],
    "event_type.attribute": ["event_type", "attribute_name"],
    "e2o_relationship": ["event_type", "related_object_type", "qualifier"],
    # una tabella con documenti di natura diversa (fatture e incassi, ordini e resi) diventa piu' tipi
    # di oggetto in base al valore di una colonna (es. tipo documento); stessa sintassi «valore = nome»
    "object_type.split": ["object_type", "activity_values"],
}


def default_qualifier(related_object_type: str | None) -> str:
    """Nome del legame evento -> oggetto quando non e' stato indicato (es. "for customer")."""
    return f"for {(related_object_type or 'object').strip().lower()}"


def qualifier_for(related_object_type: str | None, rationale=None) -> str:
    """Qualifier mancante: quello scritto per errore nella motivazione ("q: for customer"),
    altrimenti uno di default."""
    import re

    from app.i18n import render

    found = re.search(r"\bq(?:ualifier)?\s*[:=]\s*([A-Za-z][\w ]{1,40})", render("it", rationale))
    return found.group(1).strip() if found else default_qualifier(related_object_type)


def normalize_row(r: dict) -> None:
    """Riga di mapping coerente con il suo tipo: svuota i campi che il tipo non usa,
    attributo senza nome = nome della colonna, collegamento senza qualifier = uno di default."""
    keep = FIELDS_BY_ELEMENT.get(r.get("ocel_element"))
    if keep is None:
        return
    for f in ("object_type", "event_type", "attribute_name", "qualifier", "related_object_type", "activity_values"):
        if f not in keep:
            r[f] = None
    if "attribute_name" in keep and not r.get("attribute_name"):
        r["attribute_name"] = r.get("source_column")
    if r["ocel_element"] == "e2o_relationship" and not r.get("qualifier"):
        r["qualifier"] = default_qualifier(r.get("related_object_type"))


def parse_activity_values(text: str | None) -> dict[str, str] | None:
    """Testo della revisione ("valore = attivita'" per riga) -> tabella di
    traduzione. Testo vuoto = i valori sono gia' nomi di attivita' (None)."""
    if not text or not text.strip():
        return None
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        value, name = line.split("=", 1)
        if value.strip():
            out[value.strip()] = name.strip()
    return out or None


def format_activity_values(values: dict[str, str] | None) -> str:
    return "\n".join(f"{k} = {v}" for k, v in (values or {}).items())


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
        elif m["ocel_element"] == "event_type.time" and m["event_type"] in event_defs:
            ed = event_defs[m["event_type"]]
            if ed.source_table == m["source_table"] and m.get("source_column"):
                ed.time_column = m["source_column"]
        elif m["ocel_element"] == "object_type.split" and m["object_type"] in object_defs:
            od = object_defs[m["object_type"]]
            if od.source_table == m["source_table"] and m.get("source_column"):
                od.split_column = m["source_column"]
                values = m.get("activity_values") or {}
                od.split_values = {str(k).strip(): v for k, v in values.items()}
        elif m["ocel_element"] == "event_type.activity" and m["event_type"] in event_defs:
            ed = event_defs[m["event_type"]]
            if ed.source_table == m["source_table"] and m.get("source_column"):
                ed.activity_column = m["source_column"]
                values = m.get("activity_values")
                ed.activity_values = {str(k).strip(): v for k, v in values.items()} if values else None

    return object_defs, event_defs


def _object_key(obj_def: ObjectTypeDefCompiled, row: dict) -> str | None:
    values = [str(row.get(c, "")).strip() for c in obj_def.key_columns]
    if not obj_def.key_columns or any(v == "" or v == "nan" for v in values):
        return None
    return "|".join(values)


def _build_object_id(obj_def: ObjectTypeDefCompiled, row: dict) -> str | None:
    """Id dell'oggetto della riga; con la divisione per valore il prefisso e' il tipo di quella riga."""
    key = _object_key(obj_def, row)
    if key is None:
        return None
    obj_type, _ = obj_def.type_for(row)
    return f"{obj_type}:{key}" if obj_type else None


def _parse_time(raw: str | None, time_raw: str | None = None) -> datetime | None:
    """Istante dell'evento; time_raw e' l'ora da una colonna separata e si usa
    solo se la data e' senza ora (mezzanotte)."""
    if not raw or str(raw).strip() in ("", "nan"):
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y%m%d"):
        try:
            ts = datetime.strptime(str(raw).strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        hms = parse_time_of_day(time_raw)
        if hms and (ts.hour, ts.minute, ts.second) == (0, 0, 0):
            ts = ts.replace(hour=hms[0], minute=hms[1], second=hms[2])
        return ts
    return None


def _resolve_related_objects(
    event_row: dict,
    mapping: dict,
    object_defs: dict[str, ObjectTypeDefCompiled],
    tables_data: dict[str, list[dict]],
    join_index: dict | None = None,
    split_ids: dict[str, dict[str, str]] | None = None,
) -> list[str]:
    """split_ids: per i tipi divisi per valore, {tipo del mapping: {chiave: id dell'oggetto creato}}.
    Il collegamento puo' puntare al tipo del mapping (va all'oggetto con quella chiave, qualunque sia
    il suo tipo dopo la divisione) o a uno dei tipi della divisione (solo se l'oggetto e' di quel tipo)."""
    wanted = mapping["related_object_type"]
    target_def = object_defs.get(wanted)
    only = None
    if target_def is None:
        target_def = next((o for o in object_defs.values() if o.split_column and wanted in o.output_types()), None)
        only = wanted
    if target_def is None:
        return []

    def ids_for(rows) -> list[str]:
        out = []
        for r in rows:
            if target_def.split_column:
                obj_id = (split_ids or {}).get(target_def.name, {}).get(_object_key(target_def, r) or "")
                if obj_id and (only is None or obj_id.startswith(only + ":")):
                    out.append(obj_id)
            else:
                obj_id = _build_object_id(target_def, r)
                if obj_id:
                    out.append(obj_id)
        return out

    if all(c in event_row for c in target_def.key_columns):
        return ids_for([event_row])

    join_table = mapping["source_table"]
    join_col = mapping["source_column"]
    value = event_row.get(join_col)
    if join_index is None:
        joined_rows = [r for r in tables_data.get(join_table, []) if r.get(join_col) == value]
    else:
        # indice per (tabella, colonna) costruito una volta sola: con migliaia di eventi e righe
        # la ricerca riga per riga diventerebbe lentissima
        key = (join_table, join_col)
        if key not in join_index:
            idx: dict = {}
            for r in tables_data.get(join_table, []):
                idx.setdefault(r.get(join_col), []).append(r)
            join_index[key] = idx
        joined_rows = join_index[key].get(value, []) if value not in (None, "") else []
    return ids_for(joined_rows)


def build_ocel(
    tables_data: dict[str, list[dict]],
    confirmed: list[dict],
) -> tuple[dict, list[SkipRecord], dict]:
    object_defs, event_defs = compile_defs(confirmed)

    relationship_rules = [m for m in confirmed if m["ocel_element"] == "e2o_relationship"]

    objects: dict[str, dict] = {}
    split_ids: dict[str, dict[str, str]] = {}
    excluded_objects: dict[str, int] = {}
    unmapped_split: dict[str, dict[str, int]] = {}
    for obj_def in object_defs.values():
        for row in tables_data.get(obj_def.source_table, []):
            key = _object_key(obj_def, row)
            if key is None:
                continue
            obj_type, outcome = obj_def.type_for(row)
            if obj_type is None:
                excluded_objects[obj_def.name] = excluded_objects.get(obj_def.name, 0) + 1
                continue
            if outcome == "unmapped":
                bucket = unmapped_split.setdefault(obj_def.name, {})
                value = _clean(row.get(obj_def.split_column))
                bucket[value] = bucket.get(value, 0) + 1
            obj_id = f"{obj_type}:{key}"
            if obj_def.split_column:
                split_ids.setdefault(obj_def.name, {}).setdefault(key, obj_id)
            if obj_id in objects:
                continue
            attrs = []
            for col in obj_def.attribute_columns:
                val = row.get(col)
                if val is not None and str(val).strip() not in ("", "nan"):
                    attrs.append({"name": col, "time": "1970-01-01T00:00:00Z", "value": str(val)})
            objects[obj_id] = {"id": obj_id, "type": obj_type, "attributes": attrs}

    join_index: dict = {}
    events: list[dict] = []
    skip_log: list[SkipRecord] = []
    event_counter = 0
    # attivita' prodotte da ogni tipo di evento del mapping, con il numero di eventi
    activities: dict[str, dict[str, int]] = {}
    unmapped: dict[str, dict[str, int]] = {}
    attrs_by_activity: dict[str, list[str]] = {}

    for evt_def in event_defs.values():
        home_object_def = next((o for o in object_defs.values() if o.source_table == evt_def.source_table), None)
        own_rules = [m for m in relationship_rules if m["event_type"] == evt_def.name]
        produced = activities.setdefault(evt_def.name, {})

        for row in tables_data.get(evt_def.source_table, []):
            preview = {k: row.get(k) for k in list(row)[:4]}
            activity, outcome = evt_def.activity_for(row)
            if activity is None:
                value = _clean(row.get(evt_def.activity_column))
                skip_log.append(SkipRecord(
                    event_type=evt_def.name, source_table=evt_def.source_table,
                    reason=(f"valore '{value}' di '{evt_def.activity_column}' escluso nel mapping"
                            if outcome == "excluded" else f"colonna attività '{evt_def.activity_column}' vuota"),
                    row_preview=preview, kind="excluded" if outcome == "excluded" else "activity",
                ))
                continue
            ts = _parse_time(row.get(evt_def.timestamp_column),
                             row.get(evt_def.time_column) if evt_def.time_column else None)
            if ts is None:
                skip_log.append(SkipRecord(
                    event_type=evt_def.name, source_table=evt_def.source_table,
                    reason=f"timestamp mancante o non parsabile in colonna '{evt_def.timestamp_column}'",
                    row_preview=preview,
                ))
                continue
            home_id, home_excluded = None, False
            if home_object_def is not None:
                home_type, _ = home_object_def.type_for(row)
                if home_type is None and _object_key(home_object_def, row) is not None:
                    home_excluded = True
                else:
                    home_id = _build_object_id(home_object_def, row)

            relationships = []
            if home_id:
                relationships.append({"objectId": home_id, "qualifier": "involves"})
            for rule in own_rules:
                for target_id in _resolve_related_objects(row, rule, object_defs, tables_data, join_index, split_ids):
                    # solo verso oggetti che esistono (es. un riferimento a un ordine fuori estrazione non crea un
                    # oggetto fantasma; un numero di incasso non diventa una fattura)
                    if target_id not in objects:
                        continue
                    rel = {"objectId": target_id,
                           "qualifier": rule.get("qualifier") or default_qualifier(rule.get("related_object_type"))}
                    if rel not in relationships:  # es. piu' righe ponte verso lo stesso ordine
                        relationships.append(rel)
            if home_excluded and not relationships:
                # l'oggetto della riga e' escluso dalla divisione per valore e l'evento non riguarda nessun altro
                # oggetto: non fa parte del processo. Se invece riguarda altri oggetti (es. la registrazione di un
                # documento contabile che e' la fattura vista dalla sua riga cliente) l'evento resta, legato a loro.
                value = _clean(row.get(home_object_def.split_column))
                skip_log.append(SkipRecord(
                    event_type=evt_def.name, source_table=evt_def.source_table,
                    reason=f"valore '{value}' di '{home_object_def.split_column}' escluso nel mapping ({home_object_def.name})",
                    row_preview=preview, kind="excluded_object",
                ))
                continue

            produced[activity] = produced.get(activity, 0) + 1
            if outcome == "unmapped":
                value = _clean(row.get(evt_def.activity_column))
                bucket = unmapped.setdefault(evt_def.name, {})
                bucket[value] = bucket.get(value, 0) + 1
            attrs_by_activity.setdefault(activity, list(evt_def.attribute_columns))
            event_counter += 1
            event_id = f"e{event_counter}"

            attrs = []
            for col in evt_def.attribute_columns:
                val = row.get(col)
                if val is not None and str(val).strip() not in ("", "nan"):
                    attrs.append({"name": col, "value": str(val)})

            events.append({
                "id": event_id,
                "type": activity,
                "time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "attributes": attrs,
                "relationships": relationships,
            })

    events.sort(key=lambda e: e["time"])

    ocel = {
        # con la divisione per valore: i tipi effettivamente prodotti, con gli attributi della definizione
        "objectTypes": [
            {"name": t, "attributes": [{"name": c, "type": "string"} for c in od.attribute_columns]}
            for od in object_defs.values() for t in od.output_types()
            if not od.split_column or any(o["type"] == t for o in objects.values())
        ],
        # un tipo di evento senza colonna attivita' compare sempre, anche senza eventi
        "eventTypes": [
            {"name": name, "attributes": [{"name": c, "type": "string"} for c in cols]}
            for name, cols in {
                **{ed.name: ed.attribute_columns for ed in event_defs.values() if not ed.activity_column},
                **attrs_by_activity,
            }.items()
        ],
        "objects": list(objects.values()),
        "events": events,
    }

    stats = {
        "object_count": len(objects),
        "event_count": len(events),
        "object_types": len(ocel["objectTypes"]),
        "event_types": len(ocel["eventTypes"]),
        "skipped_count": sum(1 for s in skip_log if s.kind not in ("excluded", "excluded_object")),
        # per tipo di evento del mapping: attivita' prodotte con il numero di eventi,
        # e i valori della colonna attivita' tenuti senza traduzione
        "activities": {k: v for k, v in activities.items() if v},
        "unmapped_activity_values": unmapped,
        # divisione per valore: tipo prodotto -> tipo del mapping; oggetti esclusi; valori senza tipo
        "object_subtypes": {t: od.name for od in object_defs.values() if od.split_column for t in od.output_types()},
        "excluded_objects": excluded_objects,
        "unmapped_split_values": unmapped_split,
    }
    return ocel, skip_log, stats


def _event_key(e: dict) -> tuple:
    """Identita' di un evento indipendente dall'id: gli id (e1, e2, ...) sono
    progressivi per singola generazione, quindi non servono a riconoscere lo
    stesso evento in due caricamenti diversi."""
    return (
        e["type"], e["time"],
        tuple(sorted((r["objectId"], r["qualifier"]) for r in e["relationships"])),
        tuple(sorted((a["name"], a["value"]) for a in e["attributes"])),
    )


def merge_ocel(previous: dict, new: dict) -> tuple[dict, dict]:
    """Aggiornamento incrementale: unisce il log precedente con quello appena
    generato dai nuovi dati.

    - oggetti: per id; se un oggetto c'e' in entrambi vincono gli attributi
      del nuovo caricamento (dati piu' recenti)
    - eventi: aggiunti solo quelli non gia' presenti (stesso tipo, istante,
      oggetti collegati e attributi), poi riordinati e rinumerati
    Ritorna (ocel_unito, {"added_objects", "updated_objects", "added_events"}).
    """
    objects = {o["id"]: o for o in previous.get("objects", [])}
    added_objects = sum(1 for o in new["objects"] if o["id"] not in objects)
    updated_objects = sum(1 for o in new["objects"] if o["id"] in objects and objects[o["id"]] != o)
    for o in new["objects"]:
        objects[o["id"]] = o

    seen = {_event_key(e) for e in previous.get("events", [])}
    events = list(previous.get("events", []))
    added_events = 0
    for e in new["events"]:
        key = _event_key(e)
        if key not in seen:
            seen.add(key)
            events.append(e)
            added_events += 1
    events.sort(key=lambda e: e["time"])
    events = [{**e, "id": f"e{i}"} for i, e in enumerate(events, start=1)]

    merged = {
        "objectTypes": new["objectTypes"],
        "eventTypes": new["eventTypes"],
        "objects": list(objects.values()),
        "events": events,
    }
    return merged, {"added_objects": added_objects, "updated_objects": updated_objects, "added_events": added_events}
