"""Filtri per attributo dell'analisi: casi di partenza, oggetti collegati con tutta la loro storia, trasversali
esclusi dall'espansione; modello filtrato con gli stessi colori; tabelle per l'assistente.
Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import data_query, explorer, ocel_filter  # noqa: E402


def _ocel():
    objs = [{"id": f"O{i}", "type": "Order", "attributes": [{"name": "customer", "value": "C1" if i < 2 else "C2"}]}
            for i in range(4)]
    objs += [{"id": f"I{i}", "type": "Invoice", "attributes": []} for i in range(4)]
    objs += [{"id": "P0", "type": "Payment", "attributes": []}, {"id": "C1", "type": "Customer", "attributes": []},
             {"id": "C2", "type": "Customer", "attributes": []}]
    ev = []
    for i in range(4):
        c = "C1" if i < 2 else "C2"
        ev.append({"id": f"co{i}", "type": "Create Order", "time": f"2026-01-0{i + 1}T00:00:00",
                   "attributes": [{"name": "channel", "value": "web" if i % 2 else "shop"}],
                   "relationships": [{"objectId": f"O{i}"}, {"objectId": c}]})
        ev.append({"id": f"ci{i}", "type": "Create Invoice", "time": f"2026-01-1{i}T00:00:00", "attributes": [],
                   "relationships": [{"objectId": f"O{i}"}, {"objectId": f"I{i}"}, {"objectId": c}]})
    # il pagamento chiude la fattura I0 (cliente C1): resta con lui, anche se non e' collegato all'ordine
    ev.append({"id": "pay", "type": "Clear Invoice", "time": "2026-02-01T00:00:00", "attributes": [],
               "relationships": [{"objectId": "I0"}, {"objectId": "P0"}]})
    return {"objects": objs, "events": ev}


def test_object_attribute_filter_keeps_linked_histories_not_other_customers():
    out = ocel_filter.apply(_ocel(), [{"kind": "object", "type": "Order", "attribute": "customer", "values": ["C1"]}],
                            hubs={"Customer"})
    ids = {o["id"] for o in out["objects"]}
    assert ids == {"O0", "O1", "I0", "I1", "C1"}
    assert {e["id"] for e in out["events"]} == {"co0", "co1", "ci0", "ci1", "pay"}
    pay = next(e for e in out["events"] if e["id"] == "pay")
    assert [r["objectId"] for r in pay["relationships"]] == ["I0"]   # P0 non e' un caso del cliente: fuori


def test_event_attribute_filter_and_catalog():
    cat = ocel_filter.catalog(_ocel())
    assert {(a["kind"], a["type"], a["attribute"]) for a in cat} == {("object", "Order", "customer"), ("event", None, "channel")}
    out = ocel_filter.apply(_ocel(), [{"kind": "event", "attribute": "channel", "values": ["web"]}], hubs={"Customer"})
    assert {o["id"] for o in out["objects"] if o["type"] == "Order"} == {"O1", "O3"}


def test_filtered_model_keeps_colors_and_tables_for_the_assistant(tmp_path):
    p = tmp_path / "x.ocel.json"
    p.write_text(json.dumps(_ocel()))
    base = explorer.load_model(p)
    f = [{"kind": "object", "type": "Order", "attribute": "customer", "values": ["C2"]}]
    m = explorer.load_model(p, f)
    assert m.object_counts["Order"] == 2 and m.colors["Order"] == base.colors["Order"]
    t = ocel_filter.tables(ocel_filter.apply(_ocel(), f, {"Customer"}))
    res = data_query.query_rows(t, {"table": "Order", "group_by": ["customer", "activities"]})
    assert res["groups"] == [{"customer": "C2", "activities": "Create Order > Create Invoice", "rows": 2}]
