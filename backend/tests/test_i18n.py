"""Ogni testo mostrato all'utente deve avere la traduzione inglese.

Raccoglie i testi in italiano da template (_("...")) e codice Python (msg(),
t(), _issue(), check_name/details/rationale/label costanti, domande
dell'assessment, dizionario SAP) e verifica che siano nel catalogo inglese
con gli stessi segnaposto. Uso: python -m pytest tests/test_i18n.py
(oppure python tests/test_i18n.py per l'elenco dei mancanti).
"""
from __future__ import annotations

import ast
import re
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.i18n_en import EN  # noqa: E402

TEMPLATE_RE = re.compile(r"""_\(\s*(["'])((?:(?!\1).)+)\1""", re.S)
TEXT_KEYS = {"check_name", "details", "rationale", "label", "title", "why", "placeholder", "impact", "action", "error"}
# testi che restano uguali in inglese o che non sono frasi (codici, nomi propri)
SKIP = re.compile(r"^[\W\d_]*$|^[A-Z0-9_./ -]{1,12}$")


def _const(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_italian_text(s: str) -> bool:
    return bool(s.strip()) and not SKIP.match(s) and " " in s.strip() or s in ITALIAN_SINGLE_WORDS


ITALIAN_SINGLE_WORDS: set[str] = set()


def python_texts() -> set[str]:
    out: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        if path.name in ("i18n_en.py",):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in ("msg", "_issue", "concat") or (name == "t" and len(node.args) >= 2):
                    args = node.args[1:2] if name == "t" else (node.args[2:] if name == "_issue" else node.args)
                    for a in args:
                        if (s := _const(a)) is not None:
                            out.add(s)
            if isinstance(node, ast.keyword) and node.arg in ("rationale",) and (s := _const(node.value)):
                out.add(s)
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    key = _const(k) if k is not None else None
                    if key in TEXT_KEYS and (s := _const(v)) is not None:
                        out.add(s)
                    if key == "options" and isinstance(v, ast.List):
                        out.update(x for e in v.elts if (x := _const(e)))
    return out


def data_texts() -> set[str]:
    from app.routers.ingestion import ELEMENT_LABELS
    from app.services import assessment as A
    from app.services.deterministic_mapping import TEMPLATE_LABELS
    from app.services.sap_dictionary import SAP_TABLES

    out = set(ELEMENT_LABELS.values()) | set(TEMPLATE_LABELS.values())
    out |= set(A.SYSTEM_TYPES) | set(A.DOCUMENT_TYPES.values())
    for e in SAP_TABLES.values():
        out.add(e["label"])
        out |= set(e["object_attributes"].values())
        for _, _, attrs in e["events"]:
            out |= set(attrs.values())
    return out


def template_texts() -> set[str]:
    out: set[str] = set()
    for path in (ROOT / "app" / "templates").glob("*.html"):
        for m in TEMPLATE_RE.finditer(path.read_text(encoding="utf-8")):
            out.add(m.group(2))
    return out


def _fields(s: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(s) if f}


def missing() -> tuple[list[str], list[str]]:
    texts = python_texts() | data_texts() | template_texts()
    need = sorted(s for s in texts if s.strip() and not SKIP.match(s))
    absent = [s for s in need if s not in EN]
    wrong = [s for s in need if s in EN and _fields(s) != _fields(EN[s])]
    return absent, wrong


def test_every_text_has_english():
    absent, wrong = missing()
    assert not absent, "Testi senza traduzione inglese:\n" + "\n".join(absent)
    assert not wrong, "Segnaposto diversi nella traduzione:\n" + "\n".join(wrong)


def test_no_duplicate_keys():
    tree = ast.parse((ROOT / "app" / "i18n_en.py").read_text(encoding="utf-8"))
    keys = [k.value for node in ast.walk(tree) if isinstance(node, ast.Dict) for k in node.keys if isinstance(k, ast.Constant)]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    assert not dup, "Voci ripetute nel catalogo:\n" + "\n".join(dup)


if __name__ == "__main__":
    absent, wrong = missing()
    print(f"{len(absent)} senza traduzione, {len(wrong)} con segnaposto diversi")
    for s in absent:
        print("MISSING", repr(s))
    for s in wrong:
        print("WRONG", repr(s))
