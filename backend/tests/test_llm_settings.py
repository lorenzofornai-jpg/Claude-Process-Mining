"""Impostazioni dell'LLM (services/llm.py): cifratura della chiave API, tetti di output, prezzi e client.
Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import llm  # noqa: E402


def test_api_key_is_encrypted_and_hidden(monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "una frase segreta qualsiasi")
    key = "sk-ant-api03-ABCDEFGHIJKLMNOP-wxyz"
    token = llm.encrypt(key)
    assert key not in token and llm.decrypt(token) == key
    assert llm.hint(key) == "…wxyz"
    monkeypatch.setenv("APP_SECRET_KEY", "un'altra chiave")
    assert llm.decrypt(token) is None          # chiave di cifratura cambiata: non si apre, nessun errore


def test_settings_drive_models_caps_prices_and_client(monkeypatch):
    import anthropic

    s = {"enabled": True, "api_key": "sk-ant-x" * 4, "base_url": "https://gw.example.com", "timeout_s": 90,
         "max_retries": 3, "mapping_budget_usd": 0.3, "prices": {"my-model": [1.5, 7.5]},
         "uses": {u: dict(llm.DEFAULT_USES[u]) for u in llm.USES}}
    s["uses"]["assistant"].update(model="my-model", effort="medium", max_tokens=2048)
    monkeypatch.setattr(llm, "_cache", s)
    assert llm.model("assistant") == "my-model" and llm.effort("assistant") == "medium"
    assert llm.max_tokens("assistant", 6000) == 2048 and llm.max_tokens("mapping", 6000) == 6000
    assert llm.price("my-model") == (1.5, 7.5) and llm.price("sconosciuto") == llm.FALLBACK_PRICE
    seen = {}
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: seen.update(kw) or "client")
    assert llm.client() == "client"
    assert seen == {"api_key": s["api_key"], "base_url": "https://gw.example.com", "timeout": 90.0, "max_retries": 3}
    s["enabled"] = False
    assert not llm.available()
    monkeypatch.setattr(llm, "_cache", None)
