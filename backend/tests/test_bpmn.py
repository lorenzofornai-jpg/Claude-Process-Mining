"""BPA: modello BPMN 2.0 di una variante (services/bpmn.py), lane dai ruoli, sincronizzazione tra le lingue.
Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app.services import bpmn  # noqa: E402

B = "{http://www.omg.org/spec/BPMN/20100524/MODEL}"
STEPS = [{"activity": "Create Purchase Order", "repeated": False}, {"activity": "Post Goods Receipt", "repeated": True},
         {"activity": "Post Supplier Invoice", "repeated": False}, {"activity": "Clear Vendor Item", "repeated": False}]


def _ocel(with_roles=True):
    ev = []
    roles = {"Create Purchase Order": "Acquisti", "Post Goods Receipt": "Magazzino",
             "Post Supplier Invoice": "Contabilità", "Clear Vendor Item": "Contabilità"}
    for i, (act, role) in enumerate(roles.items()):
        attrs = [{"name": "USNAM", "value": f"U{i}"}] + ([{"name": "org:role", "value": role}] if with_roles else [])
        ev.append({"id": f"e{i}", "type": act, "time": "2026-01-01T00:00:00Z", "attributes": attrs})
    return {"events": ev, "objects": []}


def test_variant_becomes_bpmn_with_pool_lane_and_loop():
    xml = bpmn.generate_both({"it": "Acquisti a pagamento", "en": "Purchase to Pay"}, STEPS,
                             {"Create Purchase Order": {"it": "Crea l'ordine d'acquisto", "en": "Create Purchase Order"}}, {})
    root = ET.fromstring(xml["it"])
    proc = root.find(f"{B}process")
    assert root.find(f"{B}collaboration/{B}participant").get("name") == "Acquisti a pagamento"
    lanes = proc.findall(f"{B}laneSet/{B}lane")
    assert [l.get("name") for l in lanes] == ["Ruolo da definire"]          # nessun ruolo nel dataset: una lane da rinominare
    tasks = proc.findall(f"{B}task")
    assert [t.get("name") for t in tasks][0] == "Crea l'ordine d'acquisto" and len(tasks) == 4
    assert tasks[1].find(f"{B}standardLoopCharacteristics") is not None    # «×»: ripetuta -> ciclo
    assert len(proc.findall(f"{B}sequenceFlow")) == 5 and proc.find(f"{B}startEvent") is not None
    # stessa struttura in inglese, nomi nell'altra lingua
    en = ET.fromstring(xml["en"])
    assert en.find(f"{B}collaboration/{B}participant").get("name") == "Purchase to Pay"
    assert en.find(f"{B}process/{B}laneSet/{B}lane").get("name") == "Role to be defined"
    assert bpmn.texts(xml["it"]).keys() == bpmn.texts(xml["en"]).keys()


def test_lanes_from_roles_in_the_dataset():
    lanes = bpmn.role_lanes(_ocel(), [s["activity"] for s in STEPS])
    assert lanes["Post Goods Receipt"] == "Magazzino" and lanes["Clear Vendor Item"] == "Contabilità"
    assert bpmn.role_lanes(_ocel(with_roles=False), [s["activity"] for s in STEPS]) == {}   # USNAM e' la persona, non il ruolo
    xml = bpmn.generate_both({"it": "P2P"}, STEPS, {}, lanes)["it"]
    names = [l.get("name") for l in ET.fromstring(xml).iter(f"{B}lane")]
    assert names == ["Acquisti", "Magazzino", "Contabilità"]


def test_saving_in_one_language_updates_the_other():
    xml = bpmn.generate_both({"it": "P2P", "en": "P2P"}, STEPS, {}, {})
    edited = xml["it"].replace('name="Ruolo da definire"', 'name="Ufficio acquisti"')
    # un elemento nuovo disegnato dall'utente (annotazione) e un nome cambiato
    edited = edited.replace("</bpmn:process>", '<bpmn:textAnnotation id="Note_1"><bpmn:text>Controllo a tre vie</bpmn:text>'
                                               '</bpmn:textAnnotation></bpmn:process>')
    seen = {}

    def fake(items):
        seen.update(items)
        return {k: "EN " + v for k, v in items.items()}
    other, todo = bpmn.sync_languages(edited, xml["it"], xml["en"], fake)
    assert set(todo.values()) == {"Ufficio acquisti", "Controllo a tre vie"}   # solo i testi nuovi o cambiati
    t = bpmn.texts(other)
    assert t["Lane_1:name"] == "EN Ufficio acquisti" and t["Note_1:text"] == "EN Controllo a tre vie"
    assert t["StartEvent_1:name"] == "Start"                                      # gia' tradotto: resta
    # senza LLM i testi nuovi restano come sono
    other2, _ = bpmn.sync_languages(edited, xml["it"], xml["en"], lambda items: {})
    assert bpmn.texts(other2)["Lane_1:name"] == "Ufficio acquisti"


def test_rejects_non_bpmn_and_entities():
    with pytest.raises(bpmn.InvalidBpmn):
        bpmn.validate("<html></html>")
    with pytest.raises(bpmn.InvalidBpmn):
        bpmn.validate('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><bpmn:definitions xmlns:bpmn="'
                      'http://www.omg.org/spec/BPMN/20100524/MODEL"/>')


def test_labels_without_llm_stay_the_same(monkeypatch):
    from app.services import llm
    monkeypatch.setattr(llm, "available", lambda: False)
    labels, cost = bpmn.labels_both(["Create Purchase Order"], {"Old": {"it": "Vecchio", "en": "Old"}})
    assert labels == {"Create Purchase Order": {"it": "Create Purchase Order", "en": "Create Purchase Order"}} and cost == 0
