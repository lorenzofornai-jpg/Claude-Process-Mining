"""Repository BPA (services/bpa_taxonomy.py): tassonomia dei processi, BPMN nuovi e importati dalla staging area.
Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import Base  # noqa: E402
from app import models  # noqa: E402,F401
from app.models import BpaModel, BpaOccurrence, BpaProcess, BpaSymbol  # noqa: E402
from app.services import bpa_repo, bpa_taxonomy as tax, bpmn  # noqa: E402

STEPS = [{"activity": "Create Purchase Order", "repeated": False}, {"activity": "Approve Order", "repeated": False}]
LANES = {"Create Purchase Order": "Buyer", "Approve Order": "Buyer"}


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _staging(db, config="c1", lanes=LANES):
    xml = bpmn.generate_both({"it": "P2P", "en": "P2P"}, STEPS, {}, lanes)
    m = BpaModel(ingestion_config_id=config, lead_type="Order", variant_key=config, steps=STEPS, stats={"rank": 1},
                 labels={}, lanes=lanes, xml_it=xml["it"], xml_en=xml["en"])
    db.add(m)
    db.flush()
    bpa_repo.register_generated(db, m, "tester")
    return m


def _name(v):
    return {"it": v, "en": v}


def _symbols(db, m, typ):
    return {db.get(BpaSymbol, o.symbol_id).name_en: o.symbol_id for o in db.query(BpaOccurrence).filter_by(model_id=m.id)
            if db.get(BpaSymbol, o.symbol_id).symbol_type == typ}


def test_taxonomy_codes_move_and_delete(db):
    p2p = tax.create_process(db, None, _name("Procure to Pay"), "arch")
    buy = tax.create_process(db, p2p.id, _name("Purchasing"), "arch")
    pay = tax.create_process(db, p2p.id, _name("Payments"), "arch")
    assert (p2p.code, buy.code, pay.code) == ("PR-0001", "PR-0002", "PR-0003")
    assert [x.id for x in tax.children(db, p2p.id)] == [buy.id, pay.id]
    assert not tax.move_process(db, p2p, buy.id, 0)   # dentro un suo sottoprocesso: no
    assert tax.move_process(db, pay, p2p.id, 0)
    assert [x.id for x in tax.children(db, p2p.id)] == [pay.id, buy.id]
    assert tax.move_process(db, buy, None, 99) and buy.parent_id is None and buy.position == 1
    assert tax.delete_process(db, p2p)   # ha un sottoprocesso: motivo
    tax.new_model(db, pay, "arch")
    assert tax.delete_process(db, pay)   # ha un modello: motivo
    tree = {n["code"]: n for n in tax.tree(db, "it")}
    assert tree["PR-0003"]["models"] == 1 and tree["PR-0003"]["parent"] == p2p.id


def test_names_in_both_languages():
    assert tax.both("Acquisti", "it", None, "Purchasing") == {"it": "Acquisti", "en": "Purchasing"}
    # l'inglese era stato scritto a parte (diverso dall'italiano di prima): resta
    assert tax.both("Acquisti diretti", "it", {"it": "Acquisti", "en": "Purchasing"}, "Direct") == \
        {"it": "Acquisti diretti", "en": "Purchasing"}
    assert not tax.needs_translation("Acquisti diretti", "it", {"it": "Acquisti", "en": "Purchasing"})
    assert tax.needs_translation("Acquisti", "it", None)
    assert tax.both("Acquisti", "it", {"it": "Vecchio", "en": "Vecchio"}, None) == {"it": "Acquisti", "en": "Acquisti"}


def test_import_copies_and_keeps_the_staging_model(db):
    p = tax.create_process(db, None, _name("Purchasing"), "arch")
    src = _staging(db, "c1")
    m = tax.import_model(db, src, p, "arch")
    db.commit()
    assert m.stage == "repository" and m.ingestion_config_id is None and m.source_model_id == src.id
    assert db.get(BpaModel, src.id).stage is None   # la variante resta in staging
    # stessi oggetti della variante
    assert _symbols(db, m, "activity") == _symbols(db, src, "activity")
    # un'altra variante (altro dataset) con gli stessi nomi: oggetti diversi in staging, ma importata nel repository
    # prende quelli gia' nel repository (nomi unici nel repository)
    src2 = _staging(db, "c2")
    assert _symbols(db, src2, "lane")["Buyer"] != _symbols(db, src, "lane")["Buyer"]
    m2 = tax.import_model(db, src2, p, "arch")
    db.commit()
    assert _symbols(db, m2, "lane")["Buyer"] == _symbols(db, m, "lane")["Buyer"]
    assert _symbols(db, m2, "activity") == _symbols(db, m, "activity")
    # gli eventi senza nome sono oggetti di quella sola occorrenza
    ev = [o.symbol_id for o in db.query(BpaOccurrence).filter_by(model_id=m2.id) if db.get(BpaSymbol, o.symbol_id).anonymous]
    ev_src = [o.symbol_id for o in db.query(BpaOccurrence).filter_by(model_id=src2.id) if db.get(BpaSymbol, o.symbol_id).anonymous]
    assert ev and not set(ev) & set(ev_src)
    tax.delete_model(db, m2)
    db.commit()
    assert db.get(BpaModel, src2.id) is not None and db.query(BpaOccurrence).filter_by(model_id=m2.id).count() == 0


def test_repository_names_are_checked_in_the_repository(db):
    p = tax.create_process(db, None, _name("Purchasing"), "arch")
    m = tax.import_model(db, _staging(db, "c1"), p, "arch")
    new = tax.new_model(db, p, "arch")
    db.commit()
    assert new.title == {"it": "Purchasing", "en": "Purchasing"}
    assert "Purchasing" in bpmn.texts(new.xml_it).values()   # pool con il nome del processo
    # nel modello nuovo un'attivita' chiamata come una del repository: va decisa (riutilizza / rinomina)
    xml = new.xml_en.replace("</bpmn:process>", '<bpmn:task id="T1" name="Approve Order" /></bpmn:process>')
    plan = bpa_repo.resolve(db, new, "en", xml, {})
    assert [c["existing"] for c in plan["conflicts"]] == [_symbols(db, m, "activity")["Approve Order"]]
    # un nome usato solo in staging (non nel repository): oggetto nuovo
    st = _staging(db, "c9", {"Create Purchase Order": "Clerk", "Approve Order": "Clerk"})
    assert "Clerk" in _symbols(db, st, "lane")
    xml2 = new.xml_en.replace("</bpmn:process>", '<bpmn:task id="T2" name="Clerk" /></bpmn:process>')
    xml2 = xml2.replace('name="Role to be defined"', 'name="Clerk"')
    plan2 = bpa_repo.resolve(db, new, "en", xml2, {})
    assert not plan2["conflicts"]


def test_overlay_reference_from_staging_or_repository(db):
    from app.routers.bpa import migrate_overlay_flags, reference_of, set_reference
    src = _staging(db, "c1")
    src.overlay_reference = True   # riferimento scelto prima della tabella bpa_overlay_ref
    db.commit()
    assert migrate_overlay_flags(db) == 1 and src.overlay_reference is None
    assert reference_of(db, "c1") == (src, "Order")
    # un BPMN del repository: vale per qualunque dataset, con l'oggetto guida scelto dall'analista
    p = tax.create_process(db, None, _name("Purchasing"), "arch")
    m = tax.new_model(db, p, "arch")
    set_reference(db, "c1", m, "Purchase Order", "analyst")
    set_reference(db, "c2", m, "Invoice", "analyst")
    db.commit()
    assert reference_of(db, "c1") == (m, "Purchase Order") and reference_of(db, "c2") == (m, "Invoice")
    # una variante di un altro dataset non vale come riferimento
    set_reference(db, "c2", src, None, "analyst")
    assert reference_of(db, "c2") == (None, None)
    # modello tolto dal repository: nessun riferimento
    tax.delete_model(db, m)
    db.commit()
    assert reference_of(db, "c1") == (None, None)


def test_removing_a_model_clears_its_overlay_references(db):
    from app.models import BpaOverlayRef
    from app.routers.bpa import set_reference
    p = tax.create_process(db, None, _name("Purchasing"), "arch")
    q = tax.create_process(db, None, _name("Payments"), "arch")
    m = tax.new_model(db, p, "arch")
    set_reference(db, "c1", m, "Order", "analyst")
    db.commit()
    from app.routers.bpa_repository import overlay_uses
    assert overlay_uses(db, m.id, {}) == []   # dataset c1 non esiste piu': non si mostra
    m.process_id = q.id   # spostato su un altro processo
    db.commit()
    assert [x.id for x in tax.repository_models(db, q.id)] == [m.id] and not tax.repository_models(db, p.id)
    tax.delete_model(db, m)
    db.commit()
    assert db.query(BpaOverlayRef).count() == 0
