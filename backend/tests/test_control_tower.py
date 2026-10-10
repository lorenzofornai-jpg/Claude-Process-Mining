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


def test_propose_reads_tool_or_text_and_reports_errors(monkeypatch):
    """La proposta: dallo strumento o dal JSON nel testo; risposta troncata o errore dell'API con un motivo."""
    import types

    import anthropic
    from app.i18n import render

    def block(**kw):
        return types.SimpleNamespace(**kw)

    def fake_client(content, stop="end_turn", error=None, calls=None):
        usage = block(input_tokens=1000, output_tokens=500)

        class Stream:
            def __init__(self, kw):
                (calls if calls is not None else []).append(kw)

            def __enter__(self):
                if error:
                    raise error
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                return block(content=content, stop_reason=stop, usage=usage)

        class Client:
            messages = block(stream=lambda **kw: Stream(kw))
        return lambda *a, **k: Client()

    spec = {"kpis": [{"name": "A", "object_type": "Fattura", "measure": "count"}], "sections": [{"question": "Q", "kpis": [], "charts": []}],
            "headline": []}
    calls = []
    monkeypatch.setattr(anthropic, "Anthropic", fake_client([block(type="tool_use", input=spec)], calls=calls))
    out = ct.propose("{}")
    assert out["spec"]["kpis"][0]["id"] == "k1" and out["spec"]["sections"][0]["id"] == "s1"
    assert "tool_choice" not in calls[0]    # strumento non imposto: incompatibile con il ragionamento
    import json as _json
    monkeypatch.setattr(anthropic, "Anthropic", fake_client([block(type="text", text="Ecco:\n" + _json.dumps(spec))]))
    assert ct.propose("{}")["spec"]["kpis"][0]["name"] == "A"
    monkeypatch.setattr(anthropic, "Anthropic", fake_client([block(type="text", text="...")], stop="max_tokens"))
    try:
        ct.propose("{}")
        raise AssertionError("doveva fallire")
    except ct.ProposalError as exc:
        assert "troppo lunga" in render("it", exc.reason)
    import httpx
    err = anthropic.APIStatusError("bad", response=httpx.Response(400, request=httpx.Request("POST", "https://x")), body=None)
    monkeypatch.setattr(anthropic, "Anthropic", fake_client([], error=err))
    try:
        ct.propose("{}")
        raise AssertionError("doveva fallire")
    except ct.ProposalError as exc:
        assert "400" in render("en", exc.reason)


def test_assistant_operations_and_approval_scope():
    """Le modifiche proposte dall'assistente: KPI nuovo usato subito in un grafico (anche con id gia' preso),
    grafici cambiati e tolti, KPI della sintesi; solo domande e KPI chiedono una nuova approvazione."""
    base = _spec()
    ops = [
        {"type": "add_kpi", "section_id": "s1", "summary": True,
         "kpi": {"id": "k1", "name": "Bloccate", "object_type": "Fattura", "measure": "count",
                 "condition": {"op": "activity", "activities": ["Imposta blocco sollecito"]}}},
        {"type": "add_chart", "section_id": "s1", "chart": {"kind": "trend", "kpi": "k1", "title": "Bloccate per mese"}},
        {"type": "update_chart", "chart_id": "s1c3", "changes": {"title": "Per cliente"}},
        {"type": "remove_chart", "chart_id": "s1c4"},
        {"type": "set_summary", "section_id": "s1", "kpi_ids": ["k2"]},
        {"type": "remove_chart", "chart_id": "nope"},
    ]
    spec, _ = ct.validate(base, ExplorerModel(_ocel()))   # id dei grafici assegnati come nella pagina
    new, skipped = ct.apply_operations(spec, ops)
    added = new["kpis"][-1]
    assert added["id"] == "k6" and added["name"] == "Bloccate"            # k1 era gia' preso
    charts = new["sections"][0]["charts"]
    assert charts[-1]["kpi"] == "k6" and charts[-1]["title"] == "Bloccate per mese"
    assert [c["id"] for c in charts if c.get("title") == "Per cliente"] == ["s1c3"]
    assert "s1c4" not in [c["id"] for c in charts]
    assert new["headline"] == ["k2"] and len(skipped) == 1
    r = ct.compute(new, ExplorerModel(_ocel()), "all")
    assert r["kpis"]["k6"]["value"] == 1
    # solo grafici e sintesi cambiati: le definizioni da approvare restano uguali
    only_charts, _ = ct.apply_operations(spec, ops[2:5])
    assert ct.definitions_part(only_charts) == ct.definitions_part(spec)
    assert ct.definitions_part(new) != ct.definitions_part(spec)
    changed_target, _ = ct.apply_operations(spec, [{"type": "update_kpi", "kpi_id": "k1", "changes": {"target": {"op": "<=", "value": 20}}}])
    assert ct.definitions_part(changed_target) != ct.definitions_part(spec)


def test_texts_in_two_languages():
    """Testi delle definizioni in italiano e in inglese: quelli in una lingua sola si riconoscono e si completano;
    le etichette delle categorie possono essere testi nelle due lingue; tradurre non cambia cosa e' approvato."""
    spec = _spec()
    spec["sections"][0]["question"] = {"it": "Quante fatture a mano?", "en": "How many invoices by hand?"}
    spec["sections"][0]["charts"][1]["categories"][0]["label"] = {"it": "Blocco", "en": "Block"}
    spec["kpis"][0]["name"] = {"it": "Manuali"}            # corretto a mano in italiano: manca l'inglese
    todo = ct.untranslated(spec, "it")
    texts = {v["text"] for v in todo["texts"].values()}
    assert "Manuali" in texts and "Q" not in texts and "Puntuali" in texts
    assert "Quante fatture a mano?" not in texts and todo["object_types"] == ["Fattura"]
    # risposta simulata: le traduzioni per posizione
    out = {"texts": {i: {"it": v["text"], "en": "EN " + v["text"]} for i, v in todo["texts"].items()},
           "object_names": {"Fattura": {"it": {"one": "fattura", "many": "fatture"}, "en": {"one": "invoice", "many": "invoices"}}}}
    done = ct.merge_translations(spec, out, "it")
    assert done["kpis"][0]["name"] == {"it": "Manuali", "en": "EN Manuali"}
    assert done["kpis"][2]["name"] == {"it": "Puntuali", "en": "EN Puntuali"}
    assert ct.untranslated(done, "it") == {"texts": {}, "object_types": []}
    assert ct.definitions_part(done) == ct.definitions_part(spec)
    charts = {c["id"]: c for c in ct.compute(done, ExplorerModel(_ocel()), "all")["sections"][0]["charts"]}
    assert charts["s1c2"]["items"][0] == {"label": {"it": "Blocco", "en": "Block"}, "value": 1.0}


def test_period_as_month_range():
    """Periodo scelto con i due cursori: mesi dall'inizio alla fine, confronto con i mesi precedenti della stessa
    lunghezza solo se il dataset li copre; gli estremi sono i mesi della prima e dell'ultima fattura."""
    m = ExplorerModel(_ocel())
    r = ct.compute(_spec(), m, "2026-02..2026-03")
    assert r["months"] == ["2026-02", "2026-03"] and r["previous_months"] is None   # dic-gen: dicembre manca
    assert (r["first_month"], r["last_month"]) == ("2026-01", "2026-03")
    assert (r["kpis"]["k1"]["num"], r["kpis"]["k1"]["den"]) == (2, 3)
    r = ct.compute(_spec(), m, "2026-02..2026-02")
    assert r["previous_months"] == ["2026-01"] and r["kpis"]["k1"]["previous"] == 0.0 and r["kpis"]["k1"]["value"] == 1.0
    r = ct.compute(_spec(), m, "2025-01..2027-01")      # fuori dai dati: si restringe ai mesi del dataset
    assert r["months"] == ["2026-01", "2026-02", "2026-03"]
    assert ct.compute(_spec(), m, "bad")["months"] is None   # valore non valido: tutto


def test_layout_operations_do_not_need_approval():
    """Impaginazione con l'assistente: ordine di domande, KPI e grafici, colonne, larghezze; valori non ammessi
    scartati; nessuna nuova approvazione. Barre: quanti gruppi e in che ordine."""
    spec, _ = ct.validate(_spec(), ExplorerModel(_ocel()))
    spec["sections"].append({"id": "s2", "question": "Q2", "kpis": ["k3", "k4"], "charts": []})
    ops = [{"type": "move_section", "section_id": "s2", "position": 0},
           {"type": "move_kpi", "kpi_id": "k5", "position": 0},
           {"type": "move_chart", "chart_id": "s1c3", "position": 0},
           {"type": "move_chart", "chart_id": "s1c1", "section_id": "s2"},
           {"type": "update_layout", "changes": {"chart_columns": 1, "summary_trend": False, "colors": "red"}},
           {"type": "update_chart", "chart_id": "s1c3", "changes": {"width": "full", "top": 1, "sort": "label"}}]
    new, skipped = ct.apply_operations(spec, ops)
    assert [s["id"] for s in new["sections"]] == ["s2", "s1"]
    assert new["sections"][1]["kpis"] == ["k5", "k1", "k2"]
    assert [c["id"] for c in new["sections"][1]["charts"]] == ["s1c3", "s1c2", "s1c4"]
    assert [c["id"] for c in new["sections"][0]["charts"]] == ["s1c1"]
    assert new["layout"] == {"chart_columns": 1, "summary_trend": False} and len(skipped) == 1
    assert ct.definitions_part(new) == ct.definitions_part(spec)
    bars = next(c for s in ct.compute(new, ExplorerModel(_ocel()), "all")["sections"] for c in s["charts"] if c["id"] == "s1c3")
    assert [x["label"] for x in bars["items"]] == ["Acme", "Beta"]   # almeno 3 gruppi chiesti: ce ne sono 2
