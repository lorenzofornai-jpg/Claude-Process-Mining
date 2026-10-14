"""Archivio dei simboli dei BPMN (services/bpa_repo.py): stesso nome = stesso simbolo, occorrenze in piu' modelli,
rinomina propagata, conflitti di nome, relazioni, attributi e viste SQL.
Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import Base  # noqa: E402
from app import models  # noqa: E402,F401
from app.models import BpaAttributeDef, BpaModel, BpaOccurrence, BpaRelation, BpaSymbol  # noqa: E402
from app.services import bpa_repo, bpmn  # noqa: E402

STEPS_A = [{"activity": "Create Purchase Order", "repeated": False}, {"activity": "Approve Order", "repeated": False},
           {"activity": "Post Supplier Invoice", "repeated": False}]
STEPS_B = [{"activity": "Create Purchase Order", "repeated": False}, {"activity": "Post Supplier Invoice", "repeated": False}]
LANES = {"Create Purchase Order": "Buyer", "Approve Order": "Buyer", "Post Supplier Invoice": "Accounts payable"}


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.info["engine"], s.info["factory"] = engine, Session
    yield s
    s.close()


def _model(db, steps, lanes=LANES):
    xml = bpmn.generate_both({"it": "P2P", "en": "P2P"}, steps, {}, lanes)
    m = BpaModel(ingestion_config_id="c1", lead_type="Order", variant_key=str(len(steps)), steps=steps, stats={},
                 labels={}, lanes=lanes, xml_it=xml["it"], xml_en=xml["en"])
    db.add(m)
    db.flush()
    bpa_repo.register_generated(db, m, "tester")
    db.commit()
    return m


def _sym(db, name, typ="activity"):
    return db.query(BpaSymbol).filter_by(symbol_type=typ, key_it=bpa_repo.norm(name)).one()


def test_same_name_is_the_same_symbol_across_models(db):
    a, b = _model(db, STEPS_A), _model(db, STEPS_B)
    cpo = _sym(db, "Create Purchase Order")
    assert {o.model_id for o in db.query(BpaOccurrence).filter_by(symbol_id=cpo.id)} == {a.id, b.id}
    buyer = _sym(db, "Buyer", "lane")
    assert db.query(BpaOccurrence).filter_by(symbol_id=buyer.id).count() == 2
    # eventi di inizio e fine (nomi generici «Inizio», «Fine»): un simbolo per occorrenza, non condiviso, col sottotipo
    events = db.query(BpaSymbol).filter_by(symbol_type="event").all()
    assert len(events) == 4 and all(e.anonymous for e in events)
    labels = {bpa_repo.display_name(db, e, "it") for e in events}
    assert labels == {"Evento di inizio «Inizio»", "Evento di fine «Fine»"}
    # relazioni: pool contiene lane, lane contiene attivita', attivita' in sequenza (attraverso gli eventi senza nome)
    rel = {(r.kind, db.get(BpaSymbol, r.source_id).name_it, db.get(BpaSymbol, r.target_id).name_it)
           for r in db.query(BpaRelation).filter_by(model_id=a.id)}
    assert ("contains", "P2P", "Buyer") in rel and ("contains", "Buyer", "Approve Order") in rel
    assert ("precedes", "Create Purchase Order", "Approve Order") in rel


def test_rename_propagates_and_duplicate_names_are_conflicts(db):
    a, b = _model(db, STEPS_A), _model(db, STEPS_B)
    eid = next(o.element_id for o in db.query(BpaOccurrence).filter_by(model_id=a.id)
               if o.symbol_id == _sym(db, "Create Purchase Order").id)
    # stesso simbolo in due modelli: rinominarlo in A cambia il nome anche in B
    xml = bpa_repo.set_names(a.xml_it, {eid: "Crea l'ordine"})
    plan = bpa_repo.resolve(db, a, "it", xml, {})
    assert not plan["conflicts"] and list(plan["renames"].values()) == ["Crea l'ordine"]
    other = bpa_repo.set_names(a.xml_en, {eid: "Create the order"})
    assert bpa_repo.apply(db, a, "it", xml, other, plan, "tester")["others"] == [b.id]
    db.commit()
    assert "Crea l'ordine" in bpmn.texts(db.get(BpaModel, b.id).xml_it).values()
    assert "Create the order" in bpmn.texts(db.get(BpaModel, b.id).xml_en).values()
    # nome gia' usato da un'altra attivita': l'utente deve scegliere (riusare o rinominare)
    xml2 = bpa_repo.set_names(xml, {eid: "approve order"})
    plan2 = bpa_repo.resolve(db, a, "it", xml2, {})
    assert plan2["conflicts"] and plan2["conflicts"][0]["existing"] == _sym(db, "Approve Order").id
    # scelta «riutilizza»: l'elemento diventa un'occorrenza del simbolo esistente
    plan3 = bpa_repo.resolve(db, a, "it", xml2, {eid: {"symbol": _sym(db, "Approve Order").id}})
    assert not plan3["conflicts"] and plan3["assign"][eid] == _sym(db, "Approve Order").id


def test_copy_as_new_symbol_keeps_attributes(db):
    a = _model(db, STEPS_A)
    d = BpaAttributeDef(symbol_type="activity", key="a_owner", name={"it": "Responsabile", "en": "Owner"},
                        data_type="short_text")
    db.add(d)
    src = _sym(db, "Approve Order")
    src.attrs = {"a_owner": "Mario"}
    db.commit()
    # un'attivita' incollata come simbolo nuovo (nome diverso): stessi attributi, simbolo distinto
    xml = a.xml_it.replace("</bpmn:process>", '<bpmn:task id="Copy_1" name="Approve Order (2)" /></bpmn:process>')
    plan = bpa_repo.resolve(db, a, "it", xml, {"Copy_1": {"copy_of": src.id}})
    bpa_repo.apply(db, a, "it", xml, xml, plan, "tester")
    db.commit()
    copy = _sym(db, "Approve Order (2)")
    assert copy.id != src.id and copy.attrs == {"a_owner": "Mario"}


def test_attribute_values_are_checked_by_type():
    d = BpaAttributeDef(symbol_type="activity", key="k", name={}, data_type="integer")
    assert bpa_repo.clean_value(d, "12", "it") == (12, None) and bpa_repo.clean_value(d, "x", "it")[1]
    d.data_type, d.options = "choice", [{"value": "alta"}, {"value": "bassa"}]
    assert bpa_repo.clean_value(d, "alta", "it") == ("alta", None) and bpa_repo.clean_value(d, "media", "it")[1]
    d.data_type, d.translatable = "long_text", True
    assert bpa_repo.clean_value(d, "Testo", "en", {"it": "Testo"}) == ({"it": "Testo", "en": "Testo"}, None)
    d.data_type = "date"
    assert bpa_repo.clean_value(d, "2026-10-14", "it") == ("2026-10-14", None)


def test_views_per_symbol_type(db):
    _model(db, STEPS_A)
    db.add(BpaAttributeDef(symbol_type="activity", key="a_owner", name={"it": "Responsabile", "en": "Owner"},
                           data_type="short_text"))
    _sym(db, "Approve Order").attrs = {"a_owner": "Mario"}
    db.commit()
    engine = db.info["engine"]
    bpa_repo.refresh_views(engine, db.info["factory"])
    with engine.connect() as c:
        row = c.execute(text("SELECT nome_it, occorrenze, responsabile FROM bpa_view_activity WHERE nome_it = 'Approve Order'")).one()
        assert tuple(row) == ("Approve Order", 1, "Mario")
        assert c.execute(text("SELECT COUNT(*) FROM bpa_view_relations")).scalar() > 0


def test_unnamed_gateways_are_symbols_and_naming_them_shares_them(db):
    a = _model(db, STEPS_A)
    gw = '<bpmn:exclusiveGateway id="Gw_1" /><bpmn:startEvent id="Ev_m" name="Ordine ricevuto"><bpmn:messageEventDefinition id="Md_1" /></bpmn:startEvent>'
    xml = a.xml_it.replace("</bpmn:process>", gw + "</bpmn:process>")
    plan = bpa_repo.resolve(db, a, "it", xml, {})
    bpa_repo.apply(db, a, "it", xml, xml, plan, "tester")
    db.commit()
    occ = {o.element_id: o for o in db.query(BpaOccurrence).filter_by(model_id=a.id)}
    g = db.get(BpaSymbol, occ["Gw_1"].symbol_id)
    assert g.anonymous and bpa_repo.display_name(db, g, "it") == "Gateway esclusivo (XOR)"
    ev = db.get(BpaSymbol, occ["Ev_m"].symbol_id)
    assert not ev.anonymous and bpa_repo.subtypes(db, ev, "en") == ["Start event · message"]
    # il gateway riceve un nome: diventa un simbolo con nome (stesso id); toglierlo dal modello lo elimina se anonimo
    xml2 = xml.replace('<bpmn:exclusiveGateway id="Gw_1" />', '<bpmn:exclusiveGateway id="Gw_1" name="Fattura corretta?" />')
    plan2 = bpa_repo.resolve(db, a, "it", xml2, {})
    bpa_repo.apply(db, a, "it", xml2, xml2, plan2, "tester")
    db.commit()
    assert not db.get(BpaSymbol, g.id).anonymous and db.get(BpaSymbol, g.id).name_it == "Fattura corretta?"
