"""Nomi di oggetti, eventi e attivita' nella lingua dell'utente per le proposte senza AI.
Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import names_i18n  # noqa: E402
from app.services.ai_mapping import MappingProposal  # noqa: E402


def _p(**kw):
    base = dict(source_table="T", source_column="C", ocel_element="event_type.timestamp", object_type=None, event_type=None,
                attribute_name=None, qualifier=None, related_object_type=None, confidence=0.9, rationale="",
                based_on_template=None, activity_values=None)
    base.update(kw)
    return MappingProposal(**base)


def test_known_names_follow_the_language_both_ways():
    ps = [_p(event_type="Create Sales Order", related_object_type="Sales Order"),
          _p(ocel_element="event_type.activity", event_type="Post Purchase Order History",
             activity_values={"1": "Post Goods Receipt", "9": ""}),
          _p(event_type="My Custom Step"), _p(event_type="Bkpf event")]
    names_i18n.localize_proposals(ps, "it")
    assert (ps[0].event_type, ps[0].related_object_type) == ("Crea ordine di vendita", "Ordine di vendita")
    assert ps[1].activity_values == {"1": "Registra entrata merci", "9": ""}
    assert ps[2].event_type == "My Custom Step" and ps[3].event_type == "Evento Bkpf"
    names_i18n.localize_proposals(ps, "en")
    assert ps[0].related_object_type == "Sales Order" and ps[3].event_type == "Bkpf event"
