"""Assistente della revisione con accesso ai dati in sola lettura: interrogazioni e ciclo con Claude (finto).
Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import data_query, review_assistant  # noqa: E402

TABLES = {
    "log": [{"OBJECTID": f"1000{n:010d}2026", "FNAME": f, "OLD": o, "NEW": v}
            for n, (f, o, v) in enumerate([("MANSP", "", "B"), ("MANSP", "B", ""), ("ZTERM", "NT30", "NT60"), ("MANSP", "", "B")])],
    "docs": [{"BELNR": f"{n:010d}", "BLART": "RV"} for n in range(3)],
}


def test_query_rows_counts_and_filters():
    out = data_query.query_rows(TABLES, {"table": "log", "where": [{"column": "FNAME", "op": "eq", "value": "MANSP"}],
                                         "group_by": ["OLD", "NEW"]})
    assert out["rows_matching"] == 3
    assert out["groups"][0] == {"OLD": "(vuoto)", "NEW": "B", "rows": 2}
    assert "error" in data_query.query_rows(TABLES, {"table": "log", "group_by": ["NOPE"]})
    sample = data_query.query_rows(TABLES, {"table": "log", "where": [{"column": "NEW", "op": "empty"}], "columns": ["FNAME"]})
    assert sample["sample"] == [{"FNAME": "MANSP"}]


def test_compare_columns_with_part_of_value():
    out = data_query.compare_columns(TABLES, {"table": "log", "column": "OBJECTID", "start": 4, "length": 10,
                                              "in_table": "docs", "in_column": "BELNR"})
    assert out["distinct_values"] == 4 and out["found"] == 3 and out["examples_not_found"] == ["0000000003"]


def test_ask_runs_data_queries_then_answers(monkeypatch):
    calls = []

    class Msgs:
        def create(self, **req):
            calls.append(req)
            usage = SimpleNamespace(input_tokens=100, output_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=0)
            if len(calls) == 1:
                return SimpleNamespace(usage=usage, stop_reason="tool_use", content=[
                    SimpleNamespace(type="tool_use", id="t1", name="query_rows",
                                    input={"table": "log", "group_by": ["FNAME", "NEW"]})])
            return SimpleNamespace(usage=usage, stop_reason="end_turn",
                                   content=[SimpleNamespace(type="text", text="MANSP: 1 rimozione su 3 modifiche.")])

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda: SimpleNamespace(messages=Msgs()))
    out = review_assistant.ask("{}", [{"role": "user", "content": "ci sono rimozioni del blocco?"}], TABLES)
    assert out["answer"] == "MANSP: 1 rimozione su 3 modifiche." and out["queries"] == 1 and out["actions"] == []
    result = calls[1]["messages"][-1]["content"][0]
    assert result["tool_use_id"] == "t1" and json.loads(result["content"])["distinct_groups"] == 3
