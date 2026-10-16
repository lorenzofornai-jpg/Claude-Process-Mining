"""BPA (Business Process Analysis): da una variante della Process Overview a un modello BPMN 2.0 vero
(XML con il disegno, BPMN DI), da aprire e completare nell'editor (bpmn-js).

- Una pool con il nome del processo, sempre; le lane dai ruoli del dataset (attributi degli eventi come
  role, department, org:role...) se ci sono, altrimenti una lane da rinominare.
- Evento di inizio, le attivita' della variante in sequenza (le ripetizioni consecutive «×» con il simbolo
  del ciclo, standardLoopCharacteristics), evento di fine.
- Ogni modello esiste in italiano e in inglese con la stessa struttura: quando l'utente salva in una lingua,
  l'altra prende la stessa struttura e i nomi tradotti (sync_languages).
"""
from __future__ import annotations

import io
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from xml.sax.saxutils import escape

LANGS = ("it", "en")
NS = {
    "bpmn": "http://www.omg.org/spec/BPMN/20100524/MODEL",
    "bpmndi": "http://www.omg.org/spec/BPMN/20100524/DI",
    "dc": "http://www.omg.org/spec/DD/20100524/DC",
    "di": "http://www.omg.org/spec/DD/20100524/DI",
}
DEFAULT_NAMES = {
    "lane": {"it": "Ruolo da definire", "en": "Role to be defined"},
    "start": {"it": "Inizio", "en": "Start"},
    "end": {"it": "Fine", "en": "End"},
}
# attributi degli eventi che dicono chi fa l'attivita' come ruolo o unita' (non la persona)
_ROLE_RE = re.compile(r"(^|[_:\s.\-])(role|ruolo|department|dept|reparto|funzione|function|team|group|gruppo|"
                      r"unit|unita|ufficio|office|abteilung|area|division|divisione)($|[_:\s.\-])", re.I)
MAX_LANES = 8

# misure del disegno (pixel dell'editor)
POOL_X, POOL_Y, POOL_HEAD = 160, 80, 30
LANE_H, TASK_W, TASK_H, EV, GAP = 150, 120, 80, 36, 60


def _attr(e: dict, name: str):
    for a in e.get("attributes") or []:
        if a.get("name") == name and a.get("value") not in (None, ""):
            return str(a["value"])
    return None


def role_lanes(ocel: dict, activities: list[str]) -> dict[str, str]:
    """{attivita': ruolo} dall'attributo di ruolo degli eventi piu' presente su queste attivita' (il valore
    piu' frequente per attivita'). Vuoto se il dataset non dice chi fa cosa."""
    acts = set(activities)
    names = Counter()
    for e in ocel.get("events") or []:
        if e.get("type") in acts:
            for a in e.get("attributes") or []:
                if a.get("value") not in (None, "") and _ROLE_RE.search(str(a.get("name") or "")):
                    names[a["name"]] += 1
    for name, _ in names.most_common():
        by_act: dict[str, Counter] = defaultdict(Counter)
        for e in ocel.get("events") or []:
            if e.get("type") in acts:
                v = _attr(e, name)
                if v is not None:
                    by_act[e["type"]][v] += 1
        lanes = {a: c.most_common(1)[0][0] for a, c in by_act.items()}
        if 1 <= len(set(lanes.values())) <= MAX_LANES and len(lanes) >= max(1, len(acts) // 2):
            return lanes
    return {}


def _xml_id(prefix: str, i: int) -> str:
    return f"{prefix}_{i}"


def generate(process_name: str, steps: list[dict], names: dict[str, str], lanes: dict[str, str] | None = None,
             lane_names: dict[str, str] | None = None, start_name: str = "", end_name: str = "",
             default_lane: str = "") -> str:
    """XML BPMN 2.0 di una variante. names: nome mostrato di ogni attivita' (nella lingua del modello);
    lanes: {attivita': ruolo}; lane_names: nome mostrato di ogni ruolo."""
    lanes = lanes or {}
    lane_names = lane_names or {}
    order: list[str] = []
    for s in steps:
        r = lanes.get(s["activity"])
        if r and r not in order:
            order.append(r)
    has_free = any(not lanes.get(s["activity"]) for s in steps) or not order
    lane_keys = order + ([""] if has_free else [])
    lane_of = {k: i for i, k in enumerate(lane_keys)}

    nodes = []   # (id, kind, name, lane index, x, y, w, h, loop)
    x = POOL_X + POOL_HEAD + 60

    def lane_y(i):
        return POOL_Y + i * LANE_H

    first_lane = lane_of.get(lanes.get(steps[0]["activity"], "") if steps else "", 0)
    nodes.append(("StartEvent_1", "startEvent", start_name, first_lane, x, lane_y(first_lane) + (LANE_H - EV) / 2, EV, EV, False))
    x += EV + GAP
    for i, s in enumerate(steps, 1):
        li = lane_of.get(lanes.get(s["activity"], ""), lane_of.get("", 0))
        nodes.append((_xml_id("Activity", i), "task", names.get(s["activity"], s["activity"]), li, x,
                      lane_y(li) + (LANE_H - TASK_H) / 2, TASK_W, TASK_H, bool(s.get("repeated"))))
        x += TASK_W + GAP
    last_lane = nodes[-1][3]
    nodes.append(("EndEvent_1", "endEvent", end_name, last_lane, x, lane_y(last_lane) + (LANE_H - EV) / 2, EV, EV, False))
    pool_w = x + EV + 60 - POOL_X
    pool_h = LANE_H * len(lane_keys)

    flows = [(_xml_id("Flow", i), a, b) for i, (a, b) in enumerate(zip(nodes, nodes[1:]), 1)]
    out = io.StringIO()
    w = out.write
    w('<?xml version="1.0" encoding="UTF-8"?>\n')
    w('<bpmn:definitions xmlns:bpmn="%s" xmlns:bpmndi="%s" xmlns:dc="%s" xmlns:di="%s" '
      'id="Definitions_1" targetNamespace="http://bpmn.io/schema/bpmn" exporter="AI Process Intelligence" exporterVersion="1">\n'
      % (NS["bpmn"], NS["bpmndi"], NS["dc"], NS["di"]))
    w('  <bpmn:collaboration id="Collaboration_1">\n')
    w('    <bpmn:participant id="Participant_1" name="%s" processRef="Process_1" />\n' % escape(process_name, {'"': "&quot;"}))
    w('  </bpmn:collaboration>\n')
    w('  <bpmn:process id="Process_1" isExecutable="false">\n')
    w('    <bpmn:laneSet id="LaneSet_1">\n')
    for i, k in enumerate(lane_keys, 1):
        nm = lane_names.get(k, k) if k else default_lane
        w('      <bpmn:lane id="%s" name="%s">\n' % (_xml_id("Lane", i), escape(nm, {'"': "&quot;"})))
        for n in nodes:
            if n[3] == i - 1:
                w('        <bpmn:flowNodeRef>%s</bpmn:flowNodeRef>\n' % n[0])
        w('      </bpmn:lane>\n')
    w('    </bpmn:laneSet>\n')
    inc = {b[0]: f for f, a, b in flows}
    outg = {a[0]: f for f, a, b in flows}
    for n in nodes:
        attrs = ' name="%s"' % escape(n[2], {'"': "&quot;"}) if n[2] else ""
        w('    <bpmn:%s id="%s"%s>\n' % (n[1], n[0], attrs))
        if n[0] in inc:
            w('      <bpmn:incoming>%s</bpmn:incoming>\n' % inc[n[0]])
        if n[0] in outg:
            w('      <bpmn:outgoing>%s</bpmn:outgoing>\n' % outg[n[0]])
        if n[8]:
            w('      <bpmn:standardLoopCharacteristics />\n')
        w('    </bpmn:%s>\n' % n[1])
    for f, a, b in flows:
        w('    <bpmn:sequenceFlow id="%s" sourceRef="%s" targetRef="%s" />\n' % (f, a[0], b[0]))
    w('  </bpmn:process>\n')
    w('  <bpmndi:BPMNDiagram id="BPMNDiagram_1">\n')
    w('    <bpmndi:BPMNPlane id="BPMNPlane_1" bpmnElement="Collaboration_1">\n')
    w('      <bpmndi:BPMNShape id="Participant_1_di" bpmnElement="Participant_1" isHorizontal="true">\n')
    w('        <dc:Bounds x="%d" y="%d" width="%d" height="%d" />\n' % (POOL_X, POOL_Y, pool_w, pool_h))
    w('      </bpmndi:BPMNShape>\n')
    for i, _ in enumerate(lane_keys, 1):
        w('      <bpmndi:BPMNShape id="%s_di" bpmnElement="%s" isHorizontal="true">\n' % (_xml_id("Lane", i), _xml_id("Lane", i)))
        w('        <dc:Bounds x="%d" y="%d" width="%d" height="%d" />\n'
          % (POOL_X + POOL_HEAD, lane_y(i - 1), pool_w - POOL_HEAD, LANE_H))
        w('      </bpmndi:BPMNShape>\n')
    for n in nodes:
        w('      <bpmndi:BPMNShape id="%s_di" bpmnElement="%s">\n' % (n[0], n[0]))
        w('        <dc:Bounds x="%d" y="%d" width="%d" height="%d" />\n' % (n[4], n[5], n[6], n[7]))
        if n[1] != "task" and n[2]:
            w('        <bpmndi:BPMNLabel><dc:Bounds x="%d" y="%d" width="%d" height="14" /></bpmndi:BPMNLabel>\n'
              % (n[4] - 22, n[5] + EV + 6, EV + 44))
        w('      </bpmndi:BPMNShape>\n')
    for f, a, b in flows:
        x1, y1 = a[4] + a[6], a[5] + a[7] / 2
        x2, y2 = b[4], b[5] + b[7] / 2
        w('      <bpmndi:BPMNEdge id="%s_di" bpmnElement="%s">\n' % (f, f))
        pts = [(x1, y1), (x2, y2)] if abs(y1 - y2) < 1 else [(x1, y1), ((x1 + x2) / 2, y1), ((x1 + x2) / 2, y2), (x2, y2)]
        for px, py in pts:
            w('        <di:waypoint x="%d" y="%d" />\n' % (px, py))
        w('      </bpmndi:BPMNEdge>\n')
    w('    </bpmndi:BPMNPlane>\n')
    w('  </bpmndi:BPMNDiagram>\n')
    w('</bpmn:definitions>\n')
    return out.getvalue()


def generate_both(process_name: dict, steps: list[dict], labels: dict[str, dict], lanes: dict[str, str]) -> dict:
    """{"it": xml, "en": xml}. process_name e labels (attivita' e ruoli): {"it": ..., "en": ...}."""
    out = {}
    for lang in LANGS:
        def nm(k):
            v = labels.get(k) or {}
            return v.get(lang) or v.get("it") or v.get("en") or k
        out[lang] = generate(
            process_name.get(lang) or process_name.get("it") or "", steps,
            names={s["activity"]: nm(s["activity"]) for s in steps},
            lanes=lanes, lane_names={r: nm(r) for r in set(lanes.values())},
            start_name=DEFAULT_NAMES["start"][lang], end_name=DEFAULT_NAMES["end"][lang],
            default_lane=DEFAULT_NAMES["lane"][lang])
    return out


# ---------- lettura e sincronizzazione delle due lingue ----------

class InvalidBpmn(ValueError):
    pass


MAX_XML = 5_000_000


def _parse(xml: str) -> ET.ElementTree:
    if not isinstance(xml, str) or not xml.strip():
        raise InvalidBpmn("vuoto")
    if len(xml) > MAX_XML:
        raise InvalidBpmn("troppo grande")
    if re.search(r"<!DOCTYPE|<!ENTITY", xml[:4000], re.I):
        raise InvalidBpmn("DOCTYPE non ammesso")
    # i prefissi dei namespace restano quelli del file (bpmn:, bpmndi:, e quelli aggiunti dall'editor)
    for _, (prefix, uri) in ET.iterparse(io.BytesIO(xml.encode("utf-8")), events=("start-ns",)):
        if prefix:
            try:
                ET.register_namespace(prefix, uri)
            except ValueError:
                pass
    try:
        tree = ET.ElementTree(ET.fromstring(xml))
    except ET.ParseError as exc:
        raise InvalidBpmn(str(exc)) from exc
    root = tree.getroot()
    if root.tag != "{%s}definitions" % NS["bpmn"]:
        raise InvalidBpmn("non e' un documento BPMN 2.0")
    return tree


def _text_slots(root: ET.Element):
    """(id, elemento, "name"|"text") per ogni testo visibile: attributo name o testo delle annotazioni."""
    text_tag = "{%s}text" % NS["bpmn"]
    for el in root.iter():
        eid = el.get("id")
        if not eid or not str(el.tag).startswith("{%s}" % NS["bpmn"]):
            continue
        if el.get("name") is not None:
            yield eid, el, "name"
        t = el.find(text_tag)
        if t is not None:
            yield eid, t, "text"


def texts(xml: str) -> dict[str, str]:
    """{id: testo} dei nomi visibili del modello (attivita', eventi, lane, pool, frecce, annotazioni)."""
    out = {}
    for eid, el, kind in _text_slots(_parse(xml).getroot()):
        v = el.get("name") if kind == "name" else (el.text or "")
        if v.strip():
            out[f"{eid}:{kind}"] = v
    return out


def validate(xml: str) -> None:
    _parse(xml)


def sync_languages(new_xml: str, old_same: str | None, old_other: str | None, translate) -> tuple[str, dict]:
    """Il modello salvato in una lingua e quello nell'altra lingua con la stessa struttura: i testi gia' tradotti
    restano, quelli nuovi o cambiati si traducono con translate({chiave: testo}) -> {chiave: testo}.
    Ritorna (xml nell'altra lingua, testi da tradurre che sono stati passati a translate)."""
    tree = _parse(new_xml)
    before = texts(old_same) if old_same else {}
    other = texts(old_other) if old_other else {}
    slots = list(_text_slots(tree.getroot()))
    todo = {}
    for eid, el, kind in slots:
        key = f"{eid}:{kind}"
        v = el.get("name") if kind == "name" else (el.text or "")
        if not v.strip():
            continue
        if not (before.get(key) == v and key in other):
            todo[key] = v
    done = translate(todo) if todo else {}
    for eid, el, kind in slots:
        key = f"{eid}:{kind}"
        v = el.get("name") if kind == "name" else (el.text or "")
        if not v.strip():
            continue
        nv = done.get(key) if key in todo else other.get(key)
        nv = nv if isinstance(nv, str) and nv.strip() else v
        if kind == "name":
            el.set("name", nv)
        else:
            el.text = nv
    out = io.BytesIO()
    tree.write(out, encoding="utf-8", xml_declaration=True)
    return out.getvalue().decode("utf-8"), todo


# ---------- traduzioni con l'LLM (nomi brevi di processo) ----------

TRANSLATE_PROMPT = """\
Traduci etichette di un modello BPMN di processo aziendale (attività, eventi, ruoli, pool, condizioni). Per ogni
voce restituisci il testo nella lingua richiesta, breve e nello stile BPMN (attività: verbo all'infinito +
oggetto, es. «Registra la fattura» / «Post Supplier Invoice»). Mantieni sigle e codici (es. SAP, PO, GR/IR).
Rispondi SOLO con un oggetto JSON {chiave: traduzione}, nessun altro testo."""


def llm_translate(items: dict[str, str], target: str) -> tuple[dict[str, str], float]:
    """Traduce {chiave: testo} nella lingua target ("it"|"en"). Ritorna (traduzioni, costo in USD).
    Senza LLM o se la risposta non si legge: ({}, 0) e i testi restano come sono."""
    from app.services import llm
    if not items or not llm.available():
        return {}, 0.0
    lang = "italiano" if target == "it" else "inglese"
    try:
        client = llm.client()
        response = client.messages.create(
            model=llm.model("assistant"), max_tokens=llm.max_tokens("assistant", 4000), system=TRANSLATE_PROMPT,
            messages=[{"role": "user", "content": f"Lingua di arrivo: {lang}.\n" + json.dumps(items, ensure_ascii=False)}])
    except Exception as exc:   # rete, chiave, limiti: il modello si salva comunque
        print(f"BPA: traduzione non riuscita ({exc!r}).")
        return {}, 0.0
    price_in, price_out = llm.price(llm.model("assistant"))
    u = response.usage
    cost = ((u.input_tokens or 0) * price_in + (u.output_tokens or 0) * price_out) / 1_000_000
    text = "".join(getattr(b, "text", "") for b in response.content)
    start, end = text.find("{"), text.rfind("}")
    try:
        out = json.loads(text[start:end + 1]) if start >= 0 else {}
    except ValueError:
        out = {}
    return {k: str(v) for k, v in out.items() if k in items and isinstance(v, (str, int, float))}, round(cost, 5)


def labels_both(texts_: list[str], known: dict[str, dict] | None = None) -> tuple[dict[str, dict], float]:
    """{testo: {"it", "en"}} per nomi di attivita' e ruoli dal dataset (lingua di partenza qualsiasi): quelli gia'
    tradotti (known) non si richiedono. Senza LLM il testo resta uguale nelle due lingue."""
    known = known or {}
    out = {t: known[t] for t in texts_ if isinstance(known.get(t), dict) and all(known[t].get(l) for l in LANGS)}
    todo = [t for t in texts_ if t not in out]
    cost = 0.0
    if todo:
        items = {str(i): t for i, t in enumerate(todo)}
        tr = {}
        for lang in LANGS:
            tr[lang], c = llm_translate(items, lang)
            cost += c
        for i, t in enumerate(todo):
            out[t] = {lang: tr[lang].get(str(i)) or t for lang in LANGS}
    return out, cost
