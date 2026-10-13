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


def test_case_filters_start_month_and_duration():
    """Filtri sui casi dalle selezioni sui grafici: mese di inizio e tempo di attraversamento (solo l'oggetto o con
    gli oggetti collegati); un altro ordine nella stessa fattura non entra con il filtro sugli ordini."""
    ocel = _ocel()
    ocel["objects"].append({"id": "O9", "type": "Order", "attributes": []})
    ocel["events"].append({"id": "co9", "type": "Create Order", "time": "2026-03-05T00:00:00", "attributes": [],
                           "relationships": [{"objectId": "O9"}]})
    # una fattura che riguarda sia O0 (gennaio) sia O9 (marzo)
    ocel["events"].append({"id": "mix", "type": "Change Invoice", "time": "2026-03-06T00:00:00", "attributes": [],
                           "relationships": [{"objectId": "I0"}, {"objectId": "O0"}, {"objectId": "O9"}]})
    jan = ocel_filter.apply(ocel, [{"kind": "start_month", "type": "Order", "values": ["2026-01"]}], hubs={"Customer"})
    ids = {o["id"] for o in jan["objects"]}
    assert {"O0", "O1", "O2", "O3", "I0"} <= ids and "O9" not in ids
    assert all("O9" not in [r["objectId"] for r in e["relationships"]] for e in jan["events"])
    mar = ocel_filter.apply(ocel, [{"kind": "start_month", "type": "Order", "values": ["2026-03"]}], hubs={"Customer"})
    assert {o["id"] for o in mar["objects"] if o["type"] == "Order"} == {"O9"}
    # tempo dell'ordine: O0 va dal 1/1 al 6/3 (evento «mix»); con gli oggetti collegati conta anche il pagamento di I0
    long_ = ocel_filter.apply(ocel, [{"kind": "duration", "type": "Order", "scope": "object", "ranges": [[30, None]]}],
                              hubs={"Customer"})
    assert {o["id"] for o in long_["objects"] if o["type"] == "Order"} == {"O0"}
    short = ocel_filter.apply(ocel, [{"kind": "duration", "type": "Order", "scope": "object", "ranges": [[0, 10]]}],
                              hubs={"Customer"})
    assert {o["id"] for o in short["objects"] if o["type"] == "Order"} == {"O1", "O2", "O3", "O9"}
    # filtri normalizzati: valori non validi scartati, intervalli ordinati
    assert ocel_filter.clean([{"kind": "start_month", "type": "Order", "values": ["2026-1", "x"]},
                              {"kind": "duration", "type": "Order", "ranges": [[5, 2], [3, None], [1, 2]]}]) == [
        {"kind": "duration", "type": "Order", "scope": "object", "ranges": [[1.0, 2.0], [3.0, None]]}]
