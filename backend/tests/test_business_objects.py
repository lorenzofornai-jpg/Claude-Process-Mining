"""Oggetti di business prima del mapping: bozza senza costi e allineamento del mapping agli oggetti
confermati. Dati generici (ordini, resi, clienti, log di stato), non SAP.
Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.connectors.base import ColumnSchema, TableSchema  # noqa: E402
from app.services import business_objects as bo  # noqa: E402
from app.services.ai_mapping import MappingProposal  # noqa: E402
from app.services.profiling import profile_tables  # noqa: E402
from app.services.transformation import build_ocel  # noqa: E402

DATA = {
    "orders": [{"order_id": f"O{i}", "order_type": "SALE" if i % 3 else "RETURN", "customer_id": f"C{i % 2}",
                "created_on": f"2026-01-{i + 1:02d}", "amount": f"{100 + i}.5"} for i in range(9)],
    "customers": [{"customer_id": "C0", "name": "Alfa"}, {"customer_id": "C1", "name": "Beta"}],
    "order_status_log": [{"order_id": f"O{i // 2}", "status": ["Approved", "Shipped"][i % 2],
                          "changed_on": f"2026-02-{i + 1:02d}"} for i in range(12)],
}


def _schema():
    out = []
    for name, rows in DATA.items():
        cols = [ColumnSchema(name=c, inferred_type="date" if c.endswith("_on") else "string", null_ratio=0.0,
                             distinct_ratio=1.0, sample_values=[r[c] for r in rows[:3]]) for c in rows[0]]
        out.append(TableSchema(name=name, columns=cols, row_count=len(rows)))
    return out


def _profile():
    return profile_tables(DATA, {"orders": ["created_on"], "order_status_log": ["changed_on"], "customers": []})


def test_draft_objects_logs_and_master_data():
    d = bo.draft(_schema(), _profile(), {"main_object": "ordine di vendita (order)"})
    by_table = {o["table"]: o for o in d["objects"]}
    # tabella con chiave e date di fatti = oggetto (guida); suggerisce di dividerla per il tipo
    assert by_table["orders"]["role"] == "lead" and by_table["orders"]["include"]
    assert by_table["orders"]["split_hint"]["column"] == "order_type"
    # anagrafica senza date: proposta come contesto, non spuntata
    assert by_table["customers"]["role"] == "context" and not by_table["customers"]["include"]
    # log di stati: eventi di un altro oggetto, non un oggetto
    assert [(n["table"], n["becomes"]) for n in d["not_objects"]] == [("order_status_log", "events")]


def test_apply_aligns_any_mapping_to_confirmed_objects():
    def p(table, col, el, **kw):
        base = dict(source_table=table, source_column=col, ocel_element=el, object_type=None, event_type=None,
                    attribute_name=None, qualifier=None, related_object_type=None, confidence=0.6,
                    rationale="", based_on_template=None, activity_values=None)
        base.update(kw)
        return MappingProposal(**base)
    proposals = [  # come li proporrebbe un mapper che ragiona per tabelle
        p("orders", "order_id", "object_type.key", object_type="Orders"),
        p("orders", "amount", "object_type.attribute", object_type="Orders", attribute_name="amount"),
        p("orders", "created_on", "event_type.timestamp", event_type="Create Order"),
        p("orders", "customer_id", "e2o_relationship", event_type="Create Order", related_object_type="Customer"),
        p("customers", "customer_id", "object_type.key", object_type="Customer"),
        p("customers", "name", "object_type.attribute", object_type="Customer", attribute_name="name"),
        p("order_status_log", "changed_on", "event_type.timestamp", event_type="Status Change"),
        p("order_status_log", "status", "event_type.activity", event_type="Status Change"),
        p("order_status_log", "order_id", "e2o_relationship", event_type="Status Change", related_object_type="Orders"),
    ]
    confirmed = [
        {"name": "Sales Order", "table": "orders", "key": ["order_id"], "filter": {"column": "order_type", "values": ["SALE"]},
         "role": "lead", "include": True},
        {"name": "Return", "table": "orders", "key": ["order_id"], "filter": {"column": "order_type", "values": ["RETURN"]},
         "role": "needed", "include": True},
        {"name": "Customer", "table": "customers", "key": ["customer_id"], "filter": None, "role": "context", "include": False},
    ]
    out, rejected = bo.apply(proposals, confirmed, DATA, other_label="other")
    # il cliente non e' un oggetto confermato: le sue righe partono rifiutate, il collegamento diventa attributo
    assert {out[i].source_table for i in rejected} == {"customers"}
    rel = next(x for x in out if x.source_column == "customer_id" and x.source_table == "orders")
    assert rel.ocel_element == "event_type.attribute" and rel.attribute_name == "customer_id"
    split = next(x for x in out if x.ocel_element == "object_type.split")
    assert split.object_type == "Orders (other)" and split.activity_values == {"RETURN": "Return", "SALE": "Sales Order"}

    rows = [{**x.__dict__} for i, x in enumerate(out) if i not in rejected]
    ocel, _, _ = build_ocel(DATA, rows)
    assert Counter(o["type"] for o in ocel["objects"]) == {"Sales Order": 6, "Return": 3}
    # i cambi di stato del log raggiungono l'ordine giusto, qualunque tipo sia diventato
    linked = Counter(r["objectId"].split(":")[0] for e in ocel["events"] if e["type"] in ("Approved", "Shipped")
                     for r in e["relationships"])
    assert linked == {"Return": 4, "Sales Order": 8}


def test_confirmed_name_used_on_another_table_does_not_create_objects():
    """Il mapper chiama «Return» anche le righe del log (tabella che non ospita oggetti): niente oggetti finti,
    ma i collegamenti a «Return» restano collegamenti."""
    def p(table, col, el, **kw):
        base = dict(source_table=table, source_column=col, ocel_element=el, object_type=None, event_type=None,
                    attribute_name=None, qualifier=None, related_object_type=None, confidence=0.6,
                    rationale="", based_on_template=None, activity_values=None)
        base.update(kw)
        return MappingProposal(**base)
    proposals = [
        p("orders", "order_id", "object_type.key", object_type="Sales Order"),
        p("orders", "created_on", "event_type.timestamp", event_type="Create Order"),
        p("order_status_log", "order_id", "object_type.key", object_type="Return"),
        p("order_status_log", "changed_on", "event_type.timestamp", event_type="Status Change"),
        p("order_status_log", "order_id", "e2o_relationship", event_type="Status Change", related_object_type="Return"),
    ]
    confirmed = [
        {"name": "Sales Order", "table": "orders", "key": ["order_id"], "filter": {"column": "order_type", "values": ["SALE"]},
         "role": "lead", "include": True},
        {"name": "Return", "table": "orders", "key": ["order_id"], "filter": {"column": "order_type", "values": ["RETURN"]},
         "role": "needed", "include": True},
    ]
    out, rejected = bo.apply(proposals, confirmed, DATA)
    assert [(out[i].source_table, out[i].ocel_element) for i in rejected] == [("order_status_log", "object_type.key")]
    rel = next(x for x in out if x.ocel_element == "e2o_relationship")
    assert rel.related_object_type == "Return"
    rows = [{**x.__dict__} for i, x in enumerate(out) if i not in rejected]
    ocel, _, _ = build_ocel(DATA, rows)
    assert Counter(o["type"] for o in ocel["objects"]) == {"Sales Order": 6, "Return": 3}


def test_reference_column_links_event_to_object():
    """Una colonna con un altro nome che contiene i numeri di un oggetto confermato (il documento che ha chiuso
    la riga) collega l'evento che nasce con lei; non quello che c'e' sempre (la creazione)."""
    data = {
        "docs": [{"doc_no": f"I{i}", "kind": "INV", "posted": f"2026-01-{i + 1:02d}"} for i in range(6)]
                + [{"doc_no": f"P{i}", "kind": "PAY", "posted": f"2026-02-{i + 1:02d}"} for i in range(5)],
        "open_items": [{"doc_no": f"I{i}", "created": f"2026-01-{i + 1:02d}",
                        "closed_on": f"2026-02-{i + 1:02d}" if i < 5 else "", "closed_by_doc": f"P{i}" if i < 5 else ""}
                       for i in range(6)],
    }

    def p(table, col, el, **kw):
        base = dict(source_table=table, source_column=col, ocel_element=el, object_type=None, event_type=None,
                    attribute_name=None, qualifier=None, related_object_type=None, confidence=0.9,
                    rationale="", based_on_template=None, activity_values=None)
        base.update(kw)
        return MappingProposal(**base)
    proposals = [p("docs", "doc_no", "object_type.key", object_type="Document"),
                 p("docs", "posted", "event_type.timestamp", event_type="Post"),
                 p("open_items", "created", "event_type.timestamp", event_type="Open Item"),
                 p("open_items", "closed_on", "event_type.timestamp", event_type="Close Item"),
                 p("open_items", "doc_no", "e2o_relationship", event_type="Close Item", related_object_type="Invoice"),
                 p("open_items", "doc_no", "e2o_relationship", event_type="Open Item", related_object_type="Invoice")]
    confirmed = [
        {"name": "Invoice", "table": "docs", "key": ["doc_no"], "filter": {"column": "kind", "values": ["INV"]}, "role": "lead", "include": True},
        {"name": "Payment", "table": "docs", "key": ["doc_no"], "filter": {"column": "kind", "values": ["PAY"]}, "role": "needed", "include": True}]
    out, rejected = bo.apply(proposals, confirmed, data)
    refs = [(x.event_type, x.source_column, x.related_object_type) for x in out
            if x.ocel_element == "e2o_relationship" and x.source_column == "closed_by_doc"]
    assert refs == [("Close Item", "closed_by_doc", "Payment")]
    ocel, _, _ = build_ocel(data, [{**x.__dict__} for i, x in enumerate(out) if i not in rejected])
    close = [e for e in ocel["events"] if e["type"] == "Close Item"]
    assert len(close) == 5 and all({r["objectId"].split(":")[0] for r in e["relationships"]} == {"Invoice", "Payment"} for e in close)
    assert close[0]["relationships"][1]["objectId"] == "Payment:P0"


def test_key_of_confirmed_object_from_another_host_table_is_rejected():
    """Il mapper propone la colonna di riferimento delle righe ordine come chiave di «Return» (tipo che nasce in
    orders): niente oggetti doppi, la proposta parte rifiutata."""
    def p(table, col, el, **kw):
        base = dict(source_table=table, source_column=col, ocel_element=el, object_type=None, event_type=None,
                    attribute_name=None, qualifier=None, related_object_type=None, confidence=0.6,
                    rationale="", based_on_template=None, activity_values=None)
        base.update(kw)
        return MappingProposal(**base)
    data = dict(DATA, items=[{"item_id": f"L{i}", "order_id": f"O{i}"} for i in range(9)])
    proposals = [p("orders", "order_id", "object_type.key", object_type="Sales Order"),
                 p("items", "item_id", "object_type.key", object_type="Order Item"),
                 p("items", "order_id", "object_type.key", object_type="Return")]
    confirmed = [
        {"name": "Sales Order", "table": "orders", "key": ["order_id"], "filter": {"column": "order_type", "values": ["SALE"]}, "role": "lead", "include": True},
        {"name": "Return", "table": "orders", "key": ["order_id"], "filter": {"column": "order_type", "values": ["RETURN"]}, "role": "needed", "include": True},
        {"name": "Order Item", "table": "items", "key": ["item_id"], "filter": None, "role": "context", "include": True}]
    out, rejected = bo.apply(proposals, confirmed, data)
    assert [(out[i].source_table, out[i].source_column) for i in rejected] == [("items", "order_id")]


def test_included_objects_link_check_follows_indirect_links():
    """Posizione d'ordine (guida) -> fattura -> pagamento: il pagamento si raggiunge passando dalla fattura.
    Contano tutti gli oggetti inclusi; quelli esclusi no."""
    from app.services.validation import _check_needed_links
    objects = [{"id": f"I{i}", "type": "Item"} for i in range(4)] + [{"id": "F0", "type": "Invoice"}, {"id": "P0", "type": "Payment"},
                                                                     {"id": "S0", "type": "Supplier"}]
    events = [{"relationships": [{"objectId": "I0"}, {"objectId": "F0"}]}, {"relationships": [{"objectId": "I1"}, {"objectId": "F0"}]},
              {"relationships": [{"objectId": "F0"}, {"objectId": "P0"}]}, {"relationships": [{"objectId": "I2"}]}]
    bos = [{"name": "Item", "role": "lead", "include": True}, {"name": "Payment", "role": "needed", "include": True},
           {"name": "Supplier", "role": "context", "include": False}]
    [check] = _check_needed_links({"objects": objects, "events": events}, bos)
    assert check["affected_count"] == 0 and check["passed"]            # 2 su 4: la meta', non meno
    assert check["details"]["list"][0]["p"]["n"] == 2
    bos[2]["include"] = True                                           # nessuna posizione raggiunge il fornitore
    [check] = _check_needed_links({"objects": objects, "events": events}, bos)
    assert not check["passed"] and check["affected_count"] == 1


def test_user_added_objects_survive_a_new_claude_proposal():
    claude = [{"name": "Invoice", "table": "docs", "key": ["doc_no"], "role": "lead", "include": True, "why": "c"},
              {"name": "item", "table": "items", "key": ["item_id"], "role": "context", "include": False, "why": "Claude"}]
    user = [{"name": "Item", "table": "items", "key": ["item_id", "order_id"], "filter": None, "role": "needed",
             "include": True, "why": "Aggiunto da te.", "user_added": True},
            {"name": "Return", "table": "orders", "key": ["order_id"], "filter": None, "role": "needed", "include": True,
             "user_added": True}]
    out = bo.keep_user_objects(claude, user)
    assert [o["name"] for o in out] == ["Invoice", "Item", "Return"]
    item = out[1]   # stesso nome: chiave dell'utente, ruolo e motivazione di Claude, resta incluso
    assert item["key"] == ["item_id", "order_id"] and item["role"] == "context" and item["include"] and item["why"] == "Claude"
    assert all(o.get("user_added") for o in out[1:])
