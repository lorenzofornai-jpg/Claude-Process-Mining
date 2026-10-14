"""Process Overlay (services/overlay.py): il BPMN di riferimento confrontato con i casi del dataset.
Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import bpmn, explorer, overlay  # noqa: E402

STEPS = [{"activity": "Create Order", "repeated": False}, {"activity": "Approve Order", "repeated": False},
         {"activity": "Ship Order", "repeated": False}]


def _ocel():
    paths = {"O1": ["Create Order", "Approve Order", "Ship Order"], "O2": ["Create Order", "Ship Order"],
             "O3": ["Create Order", "Rework Order", "Approve Order", "Ship Order"],
             "O4": ["Create Order", "Approve Order", "Ship Order"]}
    objs = [{"id": o, "type": "Order", "attributes": []} for o in paths]
    ev = []
    for o, acts in paths.items():
        for i, a in enumerate(acts):
            ev.append({"id": f"{o}-{i}", "type": a, "time": f"2026-01-{int(o[1]) + 1:02d}T0{i}:00:00", "attributes": [],
                       "relationships": [{"objectId": o}]})
    return {"objects": objs, "events": ev}


def test_overlay_marks_matches_misses_and_deviations(tmp_path):
    p = tmp_path / "x.ocel.json"
    p.write_text(json.dumps(_ocel()))
    model = explorer.load_model(p)
    xml = bpmn.generate_both({"it": "P2P"}, STEPS, {"Approve Order": {"it": "Approva l'ordine", "en": "Approve Order"}}, {})["it"]
    out = overlay.compute(model, "Order", xml, STEPS, {}, "it")
    el = out["elements"]
    assert el["Activity_1"] == {"status": "ok", "count": 4, "activity": "Create Order"}
    assert el["Activity_2"]["count"] == 3          # approvato in 3 casi su 4 (task rinominato: legato per id)
    # Create -> Approve nel modello e nei dati (2 casi: O1, O4); Create -> Ship non e' nel modello: rosso
    flows = {(s, t): v for (fid, s, t) in overlay._model_graph(xml)["flows"] for v in [out["flows"][fid]]}
    assert flows[("Activity_1", "Activity_2")] == {"status": "ok", "count": 2}
    assert flows[("StartEvent_1", "Activity_1")]["count"] == 4
    reds = {(r["source"], r["target"]): r["count"] for r in out["red_edges"]}
    assert reds[("Activity_1", "Activity_3")] == 1
    # attivita' dei dati che il modello non ha
    assert [x["activity"] for x in out["extra"]] == ["Rework Order"]
    assert reds[("Activity_1", "x:Rework Order")] == 1 and reds[("x:Rework Order", "Activity_2")] == 1
    # filtro sulle varianti: solo la piu' frequente (Create, Approve, Ship: 2 casi) -> niente rosso
    top = overlay.compute(model, "Order", xml, STEPS, {}, "it", [1])
    assert top["cases_selected"] == 2 and not top["extra"] and not top["red_edges"]
