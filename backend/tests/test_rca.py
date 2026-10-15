"""Root Cause Analysis: cause note piantate in un dataset sintetico (generico, non P2P).

400 ordini di 4 regioni e 3 categorie, 5 fornitori (oggetti collegati), utenti degli eventi. Cause vere del ritardo
di consegna: il fornitore S3 (+9 giorni) e la modifica manuale della data prima della consegna (+7 giorni). Il
sollecito arriva solo dopo che l'ordine e' gia' in ritardo (conseguenza, non causa). La regione Nord ha piu' ordini
di S3 (effetto mix). Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import explorer, rca  # noqa: E402
from app.services.explorer import ExplorerModel  # noqa: E402

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)


def _a(**kw):
    return [{"name": k, "value": v} for k, v in kw.items()]


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ocel(n=400, seed=7):
    rnd = random.Random(seed)
    objects = [{"id": f"S{i}", "type": "Supplier", "attributes": _a(name=f"Supplier {i}", country=["IT", "DE"][i % 2])}
               for i in range(1, 6)]
    events = []
    for i in range(n):
        oid = f"O{i}"
        region = rnd.choice(["North", "South", "East", "West"])
        sup = "S3" if (region == "North" and rnd.random() < 0.6) or rnd.random() < 0.15 else rnd.choice(["S1", "S2", "S4", "S5"])
        cat = rnd.choice(["A", "B", "C"])
        objects.append({"id": oid, "type": "Order", "attributes": _a(region=region, category=cat,
                                                                       amount=str(rnd.randint(100, 5000)))})
        start = T0 + timedelta(days=rnd.randint(0, 300), hours=rnd.randint(0, 6))
        events.append({"id": f"c{i}", "type": "Create Order", "time": _iso(start), "attributes": _a(user=f"clerk{rnd.randint(1, 6)}"),
                       "relationships": [{"objectId": oid}, {"objectId": sup}]})
        days = rnd.uniform(3, 9)
        if sup == "S3":
            days += 9
        # conferma automatica (batch notturno) per quasi tutti; a mano per alcuni
        conf = start + timedelta(days=1)
        manual = rnd.random() < 0.18
        if not manual:
            conf = conf.replace(hour=2, minute=0, second=0)
        events.append({"id": f"k{i}", "type": "Confirm Order", "time": _iso(conf),
                       "attributes": _a(user=f"clerk{rnd.randint(1, 6)}" if manual else "BATCHJOB"),
                       "relationships": [{"objectId": oid}]})
        if manual:
            days += 7
        deliver = start + timedelta(days=days)
        events.append({"id": f"d{i}", "type": "Deliver", "time": _iso(deliver), "attributes": _a(user="WMS"),
                       "relationships": [{"objectId": oid}]})
        if days > 10:   # sollecito dopo il ritardo: conseguenza
            events.append({"id": f"r{i}", "type": "Send Reminder", "time": _iso(start + timedelta(days=10.5)),
                           "attributes": _a(user=f"clerk{rnd.randint(1, 6)}"), "relationships": [{"objectId": oid}]})
    return {"objects": objects, "events": events}


KPI_DUR = {"id": "k_lead", "object_type": "Order", "measure": "duration", "target": {"op": "<=", "value": 10},
           "duration": {"from": {"activity": "Create Order"}, "to": {"activity": "Deliver"}, "stat": "median"}}
KPI_LATE = {"id": "k_late", "object_type": "Order", "measure": "share", "target": {"op": "<=", "value": 20},
            "condition": {"op": "duration", "from": {"activity": "Create Order"}, "to": {"activity": "Deliver"},
                          "cmp": "gt", "days": 10}}


def _labels(res):
    out = []
    for f in res["findings"]:
        out.append(" & ".join(f"{p.get('k')}:{p.get('value') or p.get('activity') or p.get('attr')}" for p in f["parts"]))
    return out


def test_duration_finds_planted_causes_not_consequences():
    m = ExplorerModel(_ocel())
    res = rca.analyze(m, m, KPI_DUR, None, [])
    labels = _labels(res)
    assert res["status"] == "ok", res
    assert any("Supplier 3" in x for x in labels), labels
    assert any(x.startswith("manual:Confirm Order") for x in labels), labels
    assert not any("Send Reminder" in x for x in labels), labels       # dopo il sintomo: conseguenza
    assert not any("Deliver" in x or "Create Order" in x for x in labels), labels   # nella definizione del KPI
    assert not any("region" in x or "North" in x for x in labels), labels  # la regione e' solo un effetto del fornitore
    assert len(res["findings"]) <= rca.MAX_FINDINGS
    assert res["findings"][0]["excess"] >= res["findings"][-1]["excess"]


def test_binary_symptom_and_manual_profile():
    m = ExplorerModel(_ocel())
    prof = rca.manual_profile(m)
    assert prof["basis"] == "user" and "Confirm Order" in prof["auto"]
    assert "BATCHJOB" in prof["tech_users"]
    res = rca.analyze(m, m, KPI_LATE, None, [])
    labels = _labels(res)
    assert res["symptom"] == {"kind": "binary", "bad_is_hit": True}
    assert any("Supplier 3" in x for x in labels), labels
    assert not any("Send Reminder" in x for x in labels), labels


def test_segment_vs_rest_mix_effect():
    raw = _ocel()
    full = ExplorerModel(raw)
    sel = [{"kind": "dimension", "type": "Order", "dimension": {"source": "attribute", "attribute": "region"},
            "values": ["North"], "key": "x"}]
    seg = explorer.ExplorerModel(_filtered(raw, "North"))
    res = rca.analyze(full, seg, KPI_DUR, None, sel)
    s = res["segment"]
    assert s and s["worse"], s
    assert not any("region" in x for x in _labels(res))           # il filtro attivo non e' una causa
    assert s["n_seg"] + s["n_rest"] == 400
    # il Nord va peggio perche' ha piu' ordini del fornitore lento (mix), non perche' lavora peggio
    assert s["mix"]["type"] == "Supplier" and s["mix"]["mix_share"] > 0.7
    assert s["mix"]["top"][0]["value"].startswith("Supplier 3")


def _filtered(raw, region):
    keep = {o["id"] for o in raw["objects"] if o["type"] == "Order" and {"name": "region", "value": region} in o["attributes"]}
    evs = [e for e in raw["events"] if any(r["objectId"] in keep for r in e["relationships"])]
    used = {r["objectId"] for e in evs for r in e["relationships"]}
    return {"objects": [o for o in raw["objects"] if o["id"] in used], "events": evs}


def test_insufficient_data_is_said_not_guessed():
    m = ExplorerModel(_ocel(n=30))
    res = rca.analyze(m, m, KPI_DUR, None, [])
    assert res["status"] == "insufficient" and not res["findings"]
    assert res["notes"]


def test_random_noise_gives_no_causes():
    rnd = random.Random(3)
    objects, events = [], []
    for i in range(300):
        objects.append({"id": f"O{i}", "type": "Order", "attributes": _a(region=rnd.choice("NSEW"), category=rnd.choice("ABC"))})
        st = T0 + timedelta(days=rnd.randint(0, 300))
        events.append({"id": f"a{i}", "type": "Create Order", "time": _iso(st), "relationships": [{"objectId": f"O{i}"}]})
        events.append({"id": f"b{i}", "type": "Deliver", "time": _iso(st + timedelta(days=rnd.uniform(2, 20))),
                       "relationships": [{"objectId": f"O{i}"}]})
    m = ExplorerModel({"objects": objects, "events": events})
    res = rca.analyze(m, m, KPI_DUR, None, [])
    assert res["status"] == "none" and not res["findings"], _labels(res)


def test_fingerprint_stable_and_versioned():
    f1 = [{"kind": "dimension", "type": "Order", "values": ["North"], "dimension": {"source": "attribute", "attribute": "region"}, "title": "a"}]
    f2 = [{**f1[0], "title": "b"}]
    k1, k2 = rca.key_of("c", KPI_DUR, None, f1), rca.key_of("c", KPI_DUR, None, f2)
    assert k1 == k2   # stessi filtri, etichette diverse
    assert rca.fingerprint(k1, 1) != rca.fingerprint(k1, 2)
    assert rca.key_of("c", KPI_DUR, ["2026-01"], f1) != k1
    # il mese di inizio di un altro tipo di oggetto e' un filtro sui casi: cambia l'impronta
    other = [{"kind": "start_month", "type": "Supplier", "values": ["2026-01"]}]
    assert rca.key_of("c", KPI_DUR, None, f1 + other) != k1


def test_llm_texts_parsed_and_consequences_dismissed(monkeypatch):
    """Testi dell'LLM: un fattore giudicato conseguenza finisce tra gli scartati; i testi non hanno numeri."""
    import json
    from types import SimpleNamespace

    from app.routers.analysis import _rca_out
    from app.services import llm

    m = ExplorerModel(_ocel())
    res = rca.analyze(m, m, KPI_DUR, None, [])
    for f in res["findings"]:
        f["text_key"] = rca.text_key(KPI_DUR, f)
    payload = rca.narrate_payload(process_name="Order to Delivery", kpi={**KPI_DUR, "name": {"it": "Tempo di consegna"}},
                                  object_names={}, filter_labels=[], result=res, findings=res["findings"])
    sent = json.loads(payload)
    assert [f["id"] for f in sent["findings"]] == [f["text_key"] for f in res["findings"]]
    assert "Supplier 3" in sent["findings"][0]["factor"]

    first, second = (f["text_key"] for f in res["findings"][:2])
    block = SimpleNamespace(type="tool_use", input={"items": [
        {"id": first, "verdict": "cause", "title": {"it": "Fornitore lento", "en": "Slow supplier"},
         "explanation": {"it": "x", "en": "x"}, "check": {"it": "y", "en": "y"}},
        {"id": second, "verdict": "consequence", "title": {"it": "Conferma a mano", "en": "Manual confirmation"},
         "explanation": {"it": "x", "en": "x"}, "check": {"it": "y", "en": "y"}}]})
    fake = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(
        content=[block], usage=SimpleNamespace(input_tokens=1000, output_tokens=500))))
    monkeypatch.setattr(llm, "client", lambda: fake)
    got = rca.narrate(payload)
    assert got["items"][first]["verdict"] == "cause" and got["cost_usd"] > 0

    texts = {k: SimpleNamespace(verdict=v["verdict"], texts={x: v[x] for x in ("title", "explanation", "check")})
             for k, v in got["items"].items()}
    row = SimpleNamespace(id="r", result=res, dataset_version=1, months=None, filter_labels=[], cost_usd=0.0,
                          created_by="de", created_at=None)
    out = _rca_out(row, texts, "en", 2)
    assert out["stale"] is True
    assert [f["title"]["en"] for f in out["result"]["findings"]] == ["Slow supplier"]
    assert [f["title"]["en"] for f in out["result"]["dismissed"]] == ["Manual confirmation"]
