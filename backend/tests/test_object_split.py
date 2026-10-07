"""Trasformazione: una tabella con oggetti di business diversi (ordini e resi, fatture e incassi) diventa
piu' tipi di oggetto in base al valore di una colonna. Dati generici, non SAP.
Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.i18n import render  # noqa: E402
from app.services.transformation import build_ocel  # noqa: E402
from app.services.validation import run_data_quality_checks  # noqa: E402

TABLES = {
    "orders": [
        {"order_id": "1", "order_type": "SO", "created": "2026-01-01", "channel": "web"},
        {"order_id": "2", "order_type": "SO", "created": "2026-01-02", "channel": "shop"},
        {"order_id": "3", "order_type": "RET", "created": "2026-01-03", "channel": "web"},
        {"order_id": "4", "order_type": "TEST", "created": "2026-01-04", "channel": "web"},
        {"order_id": "5", "order_type": "XX", "created": "2026-01-05", "channel": "web"},
    ],
    "shipments": [
        {"ship_id": "S1", "order_id": "1", "shipped": "2026-01-10"},
        {"ship_id": "S3", "order_id": "3", "shipped": "2026-01-11"},
        {"ship_id": "S4", "order_id": "4", "shipped": "2026-01-12"},
    ],
}


def _rules(split_values, rel_target="Order"):
    def r(el, table, col, **kw):
        base = {"source_table": table, "source_column": col, "ocel_element": el, "object_type": None, "event_type": None,
                "attribute_name": None, "qualifier": None, "related_object_type": None, "activity_values": None}
        return {**base, **kw}
    return [
        r("object_type.key", "orders", "order_id", object_type="Order"),
        r("object_type.attribute", "orders", "channel", object_type="Order", attribute_name="channel"),
        r("object_type.split", "orders", "order_type", object_type="Order", activity_values=split_values),
        r("event_type.timestamp", "orders", "created", event_type="Create Order"),
        r("object_type.key", "shipments", "ship_id", object_type="Shipment"),
        r("event_type.timestamp", "shipments", "shipped", event_type="Ship"),
        r("e2o_relationship", "shipments", "order_id", event_type="Ship", related_object_type=rel_target, qualifier="for order"),
    ]


def test_split_creates_business_types_and_excludes_events():
    ocel, skip_log, stats = build_ocel(TABLES, _rules({"SO": "Sales Order", "RET": "Return", "TEST": ""}))
    types = Counter(o["type"] for o in ocel["objects"])
    # i valori non previsti (XX) restano nel tipo del mapping: nessun oggetto sparisce in silenzio
    assert types == {"Sales Order": 2, "Return": 1, "Order": 1, "Shipment": 3}
    assert {t["name"] for t in ocel["objectTypes"]} == {"Sales Order", "Return", "Order", "Shipment"}
    # l'evento di un oggetto escluso (TEST) non entra nel processo
    created = [e for e in ocel["events"] if e["type"] == "Create Order"]
    assert [e["relationships"][0]["objectId"] for e in created] == ["Sales Order:1", "Sales Order:2", "Return:3", "Order:5"]
    assert [s.kind for s in skip_log] == ["excluded_object"] and stats["skipped_count"] == 0
    # un collegamento al tipo del mapping va all'oggetto con quella chiave, qualunque tipo sia diventato;
    # verso un oggetto escluso non c'e' collegamento (niente riferimenti a oggetti inesistenti)
    ships = {e["time"][:10]: [r["objectId"] for r in e["relationships"] if r["qualifier"] == "for order"]
             for e in ocel["events"] if e["type"] == "Ship"}
    assert ships == {"2026-01-10": ["Sales Order:1"], "2026-01-11": ["Return:3"], "2026-01-12": []}
    assert stats["unmapped_split_values"] == {"Order": {"XX": 1}} and stats["excluded_objects"] == {"Order": 1}


def test_relation_to_one_split_type_only():
    ocel, _, _ = build_ocel(TABLES, _rules({"SO": "Sales Order", "RET": "Return"}, rel_target="Return"))
    linked = [r["objectId"] for e in ocel["events"] if e["type"] == "Ship" for r in e["relationships"]
              if r["qualifier"] == "for order"]
    assert linked == ["Return:3"]


def test_quality_report_tells_split_outcome():
    ocel, skip_log, stats = build_ocel(TABLES, _rules({"SO": "Sales Order", "RET": "Return", "TEST": ""}))
    checks = {render("it", c["check_name"]): render("it", c["details"]) for c in run_data_quality_checks(ocel, skip_log, stats)}
    assert "Order diviso in: Sales Order (2), Return (1), Order (1)" in checks["Oggetti divisi per valore (informativo)"]
    assert "XX (1 righe)" in checks["Valori senza tipo di oggetto"]
