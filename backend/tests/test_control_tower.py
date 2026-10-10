"""Control Tower: definizioni dei KPI calcolate sul dataset (quote, importi, utenti non di sistema anche negli
oggetti collegati, puntualita' rispetto alla scadenza, periodi, grafici) e validazione dei dati mancanti.
Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import control_tower as ct  # noqa: E402
from app.services.explorer import ExplorerModel  # noqa: E402


def _a(**kw):
    return [{"name": k, "value": v} for k, v in kw.items()]


def _ocel():
    """4 fatture (gen, feb, mar, mar) di due clienti; F2 ha un blocco solleciti; l'incasso di F3 e' registrato
    a mano (utente MROSSI), gli altri da SYSTEM_AUTO; F4 non e' ancora incassata."""
    objects = [{"id": "C1", "type": "Cliente", "attributes": _a(NAME1="Acme")},
               {"id": "C2", "type": "Cliente", "attributes": _a(NAME1="Beta")}]
    events = []
    months = {"F1": "2026-01", "F2": "2026-02", "F3": "2026-03", "F4": "2026-03"}
    amounts = {"F1": "100.00", "F2": "200.00", "F3": "300.00", "F4": "400.00"}
    cust = {"F1": "C1", "F2": "C1", "F3": "C2", "F4": "C2"}
    for f, m in months.items():
        objects.append({"id": f, "type": "Fattura", "attributes": _a(DMBTR=amounts[f], WAERS="EUR", ZFBDT=f"{m}-01", ZBD1T="30")})
        events.append({"id": "r" + f, "type": "Registra fattura", "time": f"{m}-01T10:00:00Z", "attributes": _a(USNAM="MROSSI"),
                       "relationships": [{"objectId": f}, {"objectId": cust[f]}]})
    events.append({"id": "b2", "type": "Imposta blocco sollecito", "time": "2026-02-05T10:00:00Z", "attributes": _a(UNAME="LFORNAI"),
                   "relationships": [{"objectId": "F2"}]})
    paid = {"F1": ("2026-01-20", "SYSTEM_AUTO"), "F2": ("2026-03-20", "SYSTEM_AUTO"), "F3": ("2026-04-10", "MROSSI")}
    for f, (d, user) in paid.items():
        objects.append({"id": "P" + f, "type": "Incasso", "attributes": []})
        events.append({"id": "i" + f, "type": "Registra incasso", "time": f"{d}T09:00:00Z", "attributes": _a(USNAM=user),
                       "relationships": [{"objectId": "P" + f}]})
        events.append({"id": "k" + f, "type": "Pareggia partita", "time": f"{d}T18:00:00Z", "attributes": [],
                       "relationships": [{"objectId": f}, {"objectId": "P" + f}]})
    return {"objects": objects, "events": events}


MANUAL = {"op": "any", "items": [
    {"op": "activity", "activities": ["Imposta blocco sollecito"]},
    {"op": "user", "activities": ["Registra incasso"], "attributes": ["USNAM"], "exclude": ["SYSTEM*"], "related": True}]}
DUE = {"attribute": "ZFBDT", "plus_days_attribute": "ZBD1T"}


def _spec():
    return {
        "headline": ["k1"],
        "kpis": [
            {"id": "k1", "name": "Manuali", "object_type": "Fattura", "measure": "share", "condition": MANUAL,
             "amount": "DMBTR", "currency": "WAERS", "target": {"op": "<=", "value": 10}},
            {"id": "k2", "name": "Importo manuale", "object_type": "Fattura", "measure": "sum", "amount": "DMBTR", "condition": MANUAL},
            {"id": "k3", "name": "Puntuali", "object_type": "Fattura", "measure": "share",
             "population": {"op": "activity", "activities": ["Pareggia partita"]},
             "condition": {"op": "duration", "from": DUE, "to": {"activity": "Pareggia partita"}, "cmp": "le", "days": 0}},
            {"id": "k4", "name": "Giorni all'incasso", "object_type": "Fattura", "measure": "duration",
             "duration": {"from": {"activity": "Registra fattura"}, "to": {"activity": "Pareggia partita"}, "stat": "median"}},
            {"id": "k5", "name": "Rotto", "object_type": "Fattura", "measure": "sum", "amount": "NETWR",
             "condition": {"op": "activity", "activities": ["Non esiste"]}},
        ],
        "sections": [{"id": "s1", "question": "Q", "kpis": ["k1", "k2", "k5"], "charts": [
            {"kind": "trend", "kpi": "k1"},
            {"kind": "donut", "kpi": "k1", "categories": [
                {"label": "Blocco", "condition": {"op": "activity", "activities": ["Imposta blocco sollecito"]}},
                {"label": "Incasso manuale", "condition": MANUAL["items"][1]}]},
            {"kind": "bars", "kpi": "k1", "dimension": {"source": "related", "object_type": "Cliente", "attribute": "NAME1"}},
            {"kind": "bars", "kpi": "k5", "dimension": {"source": "attribute", "attribute": "DMBTR"}},
        ]}],
    }


def test_kpis_on_all_data():
    m = ExplorerModel(_ocel())
    r = ct.compute(_spec(), m, "all")
    k = r["kpis"]
    # F2 (blocco) e F3 (incasso registrato a mano, nell'oggetto collegato): 2 su 4
    assert (k["k1"]["num"], k["k1"]["den"], k["k1"]["value"]) == (2, 4, 0.5)
    assert k["k1"]["amount"] == 500.0 and k["k1"]["currency"] == "EUR"
    assert k["k1"]["status"] == "ko" and k["k1"]["target"] == 0.1      # obiettivo 10 = 10%
    assert k["k2"]["value"] == 500.0
    # scadenza = data base + 30 giorni: solo F1 in tempo (F2 il 20/3, F3 il 10/4 sono in ritardo); F4 non incassata non conta
    assert (k["k3"]["num"], k["k3"]["den"]) == (1, 3)
    assert k["k4"]["value"] == 40 + 8 / 24 and k["k4"]["measured"] == 3
    assert "issues" in k["k5"] and len(k["k5"]["issues"]) == 2


def test_periods_and_comparison():
    m = ExplorerModel(_ocel())
    r = ct.compute(_spec(), m, "3")
    # i periodi finiscono all'ultimo mese in cui inizia una fattura (marzo), non con gli incassi di aprile
    assert r["months"] == ["2026-01", "2026-02", "2026-03"] and r["previous_months"] is None
    r1 = ct.compute(_spec(), m, "all")
    assert r1["months"] is None
    ctx = ct.context(m)
    cur, prev = ct.period_months(ctx, "3", "2026-06", "2026-01")
    assert cur == ["2026-04", "2026-05", "2026-06"] and prev == ["2026-01", "2026-02", "2026-03"]


def test_charts():
    m = ExplorerModel(_ocel())
    charts = {c["id"]: c for c in ct.compute(_spec(), m, "all")["sections"][0]["charts"]}
    trend = charts["s1c1"]
    assert [(p["month"], p["value"]) for p in trend["points"]] == [("2026-01", 0.0), ("2026-02", 1.0), ("2026-03", 0.5)]
    donut = charts["s1c2"]
    assert [(x["label"], x["value"]) for x in donut["items"]] == [("Blocco", 1.0), ("Incasso manuale", 1.0)]
    bars = charts["s1c3"]
    # il cliente (tipo trasversale o no) arriva dagli eventi della fattura
    assert {x["label"]: (x["num"], x["den"]) for x in bars["items"]} == {"Acme": (1, 2), "Beta": (1, 2)}
    assert charts["s1c4"].get("issues")


def test_validate_drops_unknown_references_and_keeps_shape():
    m = ExplorerModel(_ocel())
    spec, issues = ct.validate({**_spec(), "headline": ["k1", "kx"]}, m)
    assert spec["headline"] == ["k1"] and set(issues) == {"k5"}
    cat = ct.catalog(m, "Fattura")
    fattura = next(t for t in cat["types"] if t["name"] == "Fattura")
    kinds = {a["name"]: a["kind"] for a in fattura["attributes"]}
    assert kinds["DMBTR"] == "number" and kinds["ZFBDT"] == "date" and kinds["WAERS"] == "text"
    assert "USNAM" in [x["name"] for x in cat["event_attributes"]["Registra incasso"]]


def test_numbers_and_dates():
    assert ct.number("48.098,24") == 48098.24 and ct.number("1,234.50") == 1234.5 and ct.number("abc") is None
    assert ct.date_value("20260518") == ct.date_value("2026-05-18") == ct.date_value("18.05.2026")
