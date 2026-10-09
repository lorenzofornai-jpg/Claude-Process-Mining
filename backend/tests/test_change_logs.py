"""Log di modifiche e valori composti: attivita' da campo e valori, controlli sui dati, colonne calcolate nel
mapping e nel motore. Dati generici (non SAP). Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.i18n import render  # noqa: E402
from app.services import derived_columns, review_assistant  # noqa: E402
from app.services.ai_mapping import MappingProposal  # noqa: E402
from app.services.profiling import compact_for_mapping, profile_tables  # noqa: E402
from app.services.transformation import build_ocel  # noqa: E402

TICKETS = [{"ticket_id": f"T{i:04d}", "opened": f"2026-03-{i + 1:02d}", "priority": "high" if i % 2 else "low",
            "hold": "Y" if i % 3 else ""} for i in range(9)]
HISTORY = (
    [{"change_id": f"C{i}", "object_ref": f"TK-T{i:04d}-X", "changed_on": f"2026-04-{i + 1:02d}", "field": "hold",
      "old_value": "", "new_value": "Y"} for i in range(5)]
    + [{"change_id": f"C{10 + i}", "object_ref": f"TK-T{i:04d}-X", "changed_on": f"2026-05-{i + 1:02d}", "field": "hold",
        "old_value": "Y", "new_value": ""} for i in range(2)]
    + [{"change_id": f"C{20 + i}", "object_ref": f"TK-T{i:04d}-X", "changed_on": f"2026-06-{i + 1:02d}", "field": "priority",
        "old_value": "Y", "new_value": "high"} for i in range(3)]
)
DATA = {"tickets": TICKETS, "history": HISTORY}


def _profile():
    return profile_tables(DATA, {"tickets": ["opened"], "history": ["changed_on"]})


def test_profile_finds_change_log_embedded_key_and_suspicious_values():
    p = _profile()
    cm = compact_for_mapping(p)
    assert cm["change_logs"]["history"]["values"] == ["SET hold", "REMOVE hold", "CHANGE priority"]
    # il numero del ticket dentro un riferimento composto «TK-T0001-X»
    assert cm["embedded_keys"] == [{"table": "history", "column": "object_ref", "start": 3, "length": 5,
                                    "parent_table": "tickets", "parent_column": "ticket_id"}]
    titles = [render("it", i["title"]) for i in p["issues"]]
    # «Y» come valore vecchio di priority e' un valore di hold, non di priority
    assert "priority: i valori vecchi (Y) sono valori di HOLD" in titles
    assert not any("Tabella isolata" in x for x in titles)


def test_mapping_rows_and_engine_compute_activities_and_links():
    def p(table, col, el, **kw):
        base = dict(source_table=table, source_column=col, ocel_element=el, object_type=None, event_type=None,
                    attribute_name=None, qualifier=None, related_object_type=None, confidence=0.9,
                    rationale="", based_on_template=None, activity_values=None)
        base.update(kw)
        return MappingProposal(**base)
    proposals = [p("tickets", "ticket_id", "object_type.key", object_type="Ticket"),
                 p("tickets", "opened", "event_type.timestamp", event_type="Open Ticket"),
                 p("history", "changed_on", "event_type.timestamp", event_type="Ticket Change"),
                 p("history", "field", "event_type.activity", event_type="Ticket Change")]
    out = derived_columns.apply(proposals, compact_for_mapping(_profile()), "en")
    act = next(x for x in out if x.ocel_element == "event_type.activity")
    assert act.source_column == "CHANGE_KIND"
    assert act.activity_values == {"SET hold": "Set Hold", "REMOVE hold": "Remove Hold", "CHANGE priority": "Change Priority"}
    ocel, _, _ = build_ocel(DATA, [{**x.__dict__} for x in out])
    acts = Counter(e["type"] for e in ocel["events"])
    assert acts["Set Hold"] == 5 and acts["Remove Hold"] == 2 and acts["Change Priority"] == 3
    linked = [r["objectId"] for e in ocel["events"] if e["type"] == "Remove Hold" for r in e["relationships"]]
    assert linked == ["Ticket:T0000", "Ticket:T0001"]


def test_review_assistant_adds_a_change_column():
    rows = [{"row_id": 0, "source_table": "history", "source_column": "changed_on", "ocel_element": "event_type.timestamp",
             "object_type": None, "event_type": "Ticket Change", "attribute_name": None, "qualifier": None,
             "related_object_type": None, "activity_values": None, "status": "confirmed", "confidence": 0.9, "rationale": ""}]
    cols = {"history": list(HISTORY[0])}
    action = {"type": "add_computed", "rule": "change", "table": "history", "field": "field", "old": "old_value",
              "new": "new_value", "event_type": "Ticket Change", "names": {"SET hold": "Put on hold"}, "why": ""}
    d = review_assistant.describe(action, rows, cols)
    assert d and review_assistant.apply(d["action"], rows)
    assert [r["ocel_element"] for r in rows] == ["event_type.timestamp", "table.computed", "event_type.activity"]
    assert review_assistant.describe({**action, "old": "nope"}, rows, cols) is None
