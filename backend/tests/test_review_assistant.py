"""Assistente della revisione: le modifiche proposte si controllano (righe e gruppi esistenti) e si applicano solo
dopo la conferma. Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import review_assistant as ra  # noqa: E402


def _rows():
    def r(i, el, **kw):
        base = {"row_id": i, "source_table": "orders", "source_column": f"c{i}", "ocel_element": el, "object_type": None,
                "event_type": None, "attribute_name": None, "qualifier": None, "related_object_type": None,
                "activity_values": None, "status": "proposed", "confidence": 0.7, "rationale": ""}
        return {**base, **kw}
    return [r(0, "object_type.key", object_type="Orders"), r(1, "object_type.attribute", object_type="Orders"),
            r(2, "event_type.timestamp", event_type="Orders event"),
            r(3, "e2o_relationship", event_type="Ship", related_object_type="Orders"),
            r(4, "event_type.activity", event_type="Orders event", activity_values={"A": "a"})]


def test_describe_drops_unknown_targets_and_apply_changes_rows():
    rows = _rows()
    assert ra.describe({"type": "set_decisions", "changes": [{"row_id": 99, "decision": "rejected"}]}, rows) is None
    d = ra.describe({"type": "set_decisions", "why": "ok", "changes": [
        {"group_kind": "obj", "group_name": "Orders", "decision": "confirmed"}, {"group_kind": "obj", "group_name": "X", "decision": "rejected"}]}, rows)
    assert len(d["action"]["changes"]) == 1
    ra.apply(d["action"], rows)
    assert [r["status"] for r in rows[:2]] == ["confirmed", "confirmed"] and rows[2]["status"] == "proposed"

    d = ra.describe({"type": "rename", "kind": "object_type", "old": "Orders", "new": "Sales Order", "why": ""}, rows)
    ra.apply(d["action"], rows)
    assert rows[0]["object_type"] == "Sales Order" and rows[3]["related_object_type"] == "Sales Order"
    assert rows[0]["status"] == "overridden"   # una correzione su una riga gia' accettata resta tracciata

    d = ra.describe({"type": "set_values", "row_id": 4, "values": {"A": "Create Order", "B": ""}, "why": ""}, rows)
    ra.apply(d["action"], rows)
    assert rows[4]["activity_values"] == {"A": "Create Order", "B": ""}
    assert ra.describe({"type": "set_values", "row_id": 2, "values": {"x": "y"}}, rows) is None
