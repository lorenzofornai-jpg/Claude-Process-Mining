"""Overview con «oggetti collegati»: le varianti comprendono le attivita' degli oggetti collegati (qui l'incasso
che chiude la fattura), con «solo l'oggetto» no.
Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.explorer import ExplorerModel  # noqa: E402
from app.services.overview import build_overview  # noqa: E402


def _ocel():
    objects = [{"id": f"I{i}", "type": "Invoice"} for i in range(3)] + [{"id": f"P{i}", "type": "Payment"} for i in range(2)]
    events = []
    for i in range(3):
        events.append({"id": f"c{i}", "type": "Create Invoice", "time": f"2026-01-0{i + 1}T00:00:00",
                       "relationships": [{"objectId": f"I{i}"}]})
    for i in range(2):
        events.append({"id": f"p{i}", "type": "Post Payment", "time": f"2026-02-0{i + 1}T00:00:00",
                       "relationships": [{"objectId": f"P{i}"}]})
        events.append({"id": f"k{i}", "type": "Clear Invoice", "time": f"2026-02-0{i + 2}T00:00:00",
                       "relationships": [{"objectId": f"I{i}"}, {"objectId": f"P{i}"}]})
    return {"objects": objects, "events": events}


def test_related_scope_variants_include_related_activities():
    m = ExplorerModel(_ocel())
    own = build_overview(m, "Invoice", "object")
    assert [tuple(s["activity"] for s in v["steps"]) for v in own["variants"]] == [
        ("Create Invoice", "Clear Invoice"), ("Create Invoice",)]
    rel = build_overview(m, "Invoice", "related")
    assert [tuple(s["activity"] for s in v["steps"]) for v in rel["variants"]] == [
        ("Create Invoice", "Post Payment", "Clear Invoice"), ("Create Invoice",)]
    assert rel["related_adds"] == 2


def test_explorer_variant_filter_shows_only_chosen_variants():
    from app.services.explorer import build_graph
    m = ExplorerModel(_ocel())
    vs = m.variants("Invoice")
    assert [(v["id"], v["steps"], v["count"]) for v in vs] == [
        (1, ["Create Invoice", "Clear Invoice"], 2), (2, ["Create Invoice"], 1)]
    g = build_graph(m, ["Invoice", "Payment"], variant_type="Invoice", variant_ids=[2])
    assert g["types"] == ["Invoice"] and g["variants"]["objects"] == 1
    assert {(e["source"], e["target"]) for e in g["edges"]} == {("__start__|Invoice", "Create Invoice"),
                                                                ("Create Invoice", "__end__|Invoice")}
    assert [a["name"] for a in g["activity_list"]] == ["Create Invoice"]
