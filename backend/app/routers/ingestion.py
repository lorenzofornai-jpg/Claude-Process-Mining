from __future__ import annotations

import json
import shutil
import time
import uuid
import zipfile
from dataclasses import asdict, replace
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import current_user, has_process_access
from app.i18n import get_lang, joined, msg, render, setup_templates, t, to_text, ui_labels
from app.config import AI_MAPPER, AI_MAPPING_BUDGET_USD, AUTO_ACCEPT_CONFIDENCE_THRESHOLD, DATA_DIR, STATIC_VERSION
from app.connectors.file_connector import FileConnector
from app.db import SessionLocal
from app.models import (
    ObjectiveCoverage,
    Connector as ConnectorModel,
    DataQualityCheckResult,
    EventTypeDef,
    ExtractionRun,
    FieldMapping,
    IngestionConfig,
    IngestionConfigVersion,
    ObjectTypeDef,
    ProcessAssignment,
    ProcessIngestionLink,
    ProcessWorkspace,
    SourceSystem,
    User,
)
from app import state
from app.services.deterministic_mapping import TEMPLATE_LABELS
from app.services import business_objects, coverage, derived_columns, explain, names_i18n, review_assistant
from app.services.ai_mapping import AIMapper, ClaudeAIMapper, HeuristicAIMapper, MappingBudgetError, MappingProposal
from app.routers.analysis import available_datasets
from app.routers.assessment import assessment_status, documents_for_mapping, load_assessment, mapping_context
from app.services.documents import select_excerpts
from app.services.profiling import compact_for_mapping, profile_tables
from app.services.relevance import check_relevance
from app.services.structures import delete_structures, remove_files, workspace_config_ids
from app.services.transformation import (
    COMPUTED, computed_specs, with_computed,
    FIELDS_BY_ELEMENT, build_ocel, compile_defs, default_qualifier, format_activity_values, merge_ocel, normalize_row,
    parse_activity_values, qualifier_for,
)
from app.services.validation import run_data_quality_checks

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals["static_version"] = STATIC_VERSION
setup_templates(templates)


def _wizard_nav(user, workspace_id: str | None) -> dict:
    """Passi in alto cliccabili: si torna (o si va) a un passo gia' raggiunto in questa sessione."""
    if not user or not workspace_id:
        return {}
    sess = state._SESSIONS.get((user.id, workspace_id)) or {}
    nav = {}
    if sess.get("profile"):
        nav[2] = f"/ingestion/profile?workspace_id={workspace_id}"
    if sess.get("mapping_rows") is not None and sess.get("mapping_status") == "done":
        nav[3] = f"/ingestion/review?workspace_id={workspace_id}"
    if sess.get("result"):
        nav[5] = f"/ingestion/result?workspace_id={workspace_id}"
    return nav


templates.env.globals["wizard_nav"] = _wizard_nav
templates.env.filters["activity_values_text"] = format_activity_values

UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "output"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_TABLE_EXTENSIONS = {".csv", ".txt"}
MAX_ZIP_MEMBER_BYTES = 50 * 1024 * 1024  # 50 MB per file estratto: guardia contro zip bomb


def _save_uploaded_files(files: list[UploadFile], dest_dir: Path) -> list[Path]:
    """Salva i file caricati in dest_dir. Accetta CSV/TXT diretti oppure uno o
    più file .zip che li contengono: estrae automaticamente solo i membri con
    estensione consentita, scartando il percorso di cartella di ogni membro
    (mai zip-slip: il nome finale è sempre solo il basename dentro dest_dir),
    e ignora membri oltre MAX_ZIP_MEMBER_BYTES."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for f in files or []:
        if not f.filename:
            continue
        suffix = Path(f.filename).suffix.lower()
        if suffix == ".zip":
            with zipfile.ZipFile(f.file) as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    name = Path(member.filename).name
                    if not name or Path(name).suffix.lower() not in ALLOWED_TABLE_EXTENSIONS:
                        continue
                    if member.file_size > MAX_ZIP_MEMBER_BYTES:
                        continue
                    dest = dest_dir / name
                    with zf.open(member) as src, dest.open("wb") as out:
                        shutil.copyfileobj(src, out)
                    saved.append(dest)
        elif suffix in ALLOWED_TABLE_EXTENSIONS:
            dest = dest_dir / f.filename
            with dest.open("wb") as out:
                shutil.copyfileobj(f.file, out)
            saved.append(dest)
        # altre estensioni: ignorate silenziosamente
    return saved


def _get_ai_mapper() -> tuple[AIMapper, str]:
    """Ritorna (mapper, label). Default: Claude (ragiona da zero, nessun
    catalogo di tabelle note). Fallback automatico sull'euristica mock solo
    se il client Anthropic non si inizializza: evita di bloccare del tutto
    chi non ha ancora configurato credenziali, ma non e' il percorso pensato
    per l'uso normale.

    Non si controlla ANTHROPIC_API_KEY esplicitamente: l'SDK Anthropic
    risolve da solo le credenziali (api key, auth token, profilo salvato con
    `ant auth login`, o Workload Identity Federation via
    ANTHROPIC_FEDERATION_RULE_ID/ANTHROPIC_ORGANIZATION_ID/
    ANTHROPIC_SERVICE_ACCOUNT_ID/ANTHROPIC_IDENTITY_TOKEN_FILE) - un controllo
    hardcoded sulla sola env var API key darebbe un falso fallback su chi usa
    la federation invece di una chiave statica.
    """
    if AI_MAPPER == "claude":
        try:
            return ClaudeAIMapper(), "Claude (LLM reale)"
        except Exception as exc:
            print(f"AI_MAPPER=claude ma il client Anthropic non si inizializza ({exc!r}): fallback sull'euristica mock per questo upload.")
    return HeuristicAIMapper(), "euristica mock"


def _require_process_access(request: Request, workspace_id: str):
    """Ritorna (user, None) se autorizzato al Modulo 1 (Ingestion) di questo
    processo, altrimenti (None, redirect_o_403)."""
    user = current_user(request)
    if user is None:
        return None, RedirectResponse("/login", status_code=303)
    if not has_process_access(user, workspace_id, required_role="data_engineer"):
        return None, HTMLResponse(t(get_lang(request), "Accesso negato: non sei assegnato come Data Engineer a questo processo."),
            status_code=403,
        )
    return user, None


def _load_session(user_id: str, workspace_id: str) -> dict:
    """Stato in-memory per (utente, workspace), inizializzato al volo dal DB
    se e' la prima visita di questa run del server (vedi state.py)."""
    sess = state.ensure(user_id, workspace_id)
    if "context" not in sess:
        # Nome del processo + risposte dell'assessment + attivita' lette dai BPMN
        # (vedi routers/assessment.py): e' il process_context dato all'AI Mapping.
        sess["context"] = mapping_context(workspace_id)
    return sess


def _schema_fingerprint(tables_schema: list[dict]) -> dict[str, list[str]]:
    """{nome_tabella: [colonne]} usato per capire se un nuovo caricamento dati
    e' compatibile con una struttura di mapping gia' confermata."""
    return {t["name"]: sorted(c["name"] for c in t["columns"]) for t in tables_schema}


def _field_mapping_row_to_dict(r: FieldMapping) -> dict:
    """Converte una riga FieldMapping persistita nello stesso formato dict
    usato da build_ocel/compile_defs (lo stesso prodotto dalle proposte AI in
    sess["mapping_rows"]): permette di riapplicare un mapping gia' confermato
    a un nuovo caricamento dati senza rifare AI+revisione."""
    return {
        "source_table": r.source_table, "source_column": r.source_column,
        "ocel_element": r.ocel_element, "object_type": r.object_type, "event_type": r.event_type,
        "attribute_name": r.attribute_name, "qualifier": r.qualifier,
        "related_object_type": r.related_object_type, "activity_values": r.activity_values,
    }


def _check_schema_compatibility(new_fingerprint: dict[str, list[str]], confirmed: list[dict]) -> list[str]:
    """Ritorna la lista di tabelle/colonne che il mapping confermato richiede
    ma che non sono presenti nel nuovo caricamento; lista vuota = compatibile.
    Colonne extra nel nuovo caricamento non sono un problema: contano solo
    quelle effettivamente usate dal mapping."""
    # le colonne calcolate non stanno nei file: servono le colonne da cui si calcolano
    computed = {(t, c) for t, cols in computed_specs(confirmed).items() for c in cols}
    needed = {
        (r["source_table"], r["source_column"])
        for r in confirmed
        if r.get("source_table") and r.get("source_column") and (r["source_table"], r["source_column"]) not in computed
    }
    for t, cols in computed_specs(confirmed).items():
        for spec in cols.values():
            needed |= {(t, spec[k]) for k in ("field", "old", "new", "column") if spec.get(k)}
    missing_tables = {table for table, _ in needed if table not in new_fingerprint}
    problems = [msg("tabella mancante: «{t}»", t=table) for table in sorted(missing_tables)]
    for table, column in sorted(needed):
        if table not in missing_tables and column not in new_fingerprint[table]:
            problems.append(msg("colonna mancante: «{c}»", c=f"{table}.{column}"))
    return problems


@router.get("/", response_class=HTMLResponse)
def root(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse(url="/login")
    return RedirectResponse(url="/admin" if user.is_admin else "/ingestion/dashboard")


@router.get("/ingestion/dashboard", response_class=HTMLResponse)
def ingestion_dashboard(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    # L'admin non lavora sui moduli (niente strutture ne' analisi): la sua
    # "home" e' Amministrazione, "I miei processi" non ha senso per lui.
    if user.is_admin:
        return RedirectResponse("/admin", status_code=303)

    db = SessionLocal()
    try:
        assignments = db.query(ProcessAssignment).filter_by(user_id=user.id).all()
        roles_by_workspace: dict[str, set] = {}
        for a in assignments:
            roles_by_workspace.setdefault(a.workspace_id, set()).add(a.role)
        workspaces = (
            db.query(ProcessWorkspace)
            .filter(ProcessWorkspace.id.in_(roles_by_workspace.keys()))
            .order_by(ProcessWorkspace.created_at.desc())
            .all()
            if roles_by_workspace
            else []
        )
        # per il Data Analyst: «Apri analisi» e' attivo solo se c'e' almeno un dataset pronto
        datasets_count = {
            ws.id: len(available_datasets(db, ws.id)) for ws in workspaces
            if "data_analyst" in roles_by_workspace.get(ws.id, set())
        }
    finally:
        db.close()

    return templates.TemplateResponse(
        "ingestion_dashboard.html",
        {
            "request": request, "user": user, "workspaces": workspaces, "roles_by_workspace": roles_by_workspace,
            "datasets_count": datasets_count,
            "assessments": {
                ws.id: assessment_status(ws.id) for ws in workspaces
                if "data_engineer" in roles_by_workspace.get(ws.id, set())
            },
        },
    )


EDITABLE_FIELDS = ["ocel_element", "object_type", "event_type", "attribute_name", "qualifier", "related_object_type"]
VALID_OCEL_ELEMENTS = [
    "object_type.key", "object_type.attribute", "object_type.split",
    "event_type.timestamp", "event_type.time", "event_type.activity", "event_type.attribute", "e2o_relationship",
    "table.computed",
]

# come si presenta ogni tipo di riga nel pannello "Modifica" della revisione
ELEMENT_LABELS = {
    "object_type.key": "la chiave di un oggetto",
    "object_type.attribute": "un attributo di un oggetto",
    "object_type.split": "la colonna che divide l'oggetto in tipi diversi (es. fatture e incassi)",
    "event_type.timestamp": "la data di un evento",
    "event_type.time": "l'ora di un evento (unita alla sua data)",
    "event_type.activity": "la colonna che dice quale attività è avvenuta",
    "event_type.attribute": "un attributo di un evento (es. utente, importo)",
    "e2o_relationship": "un collegamento da un evento a un oggetto",
    "table.computed": "una colonna calcolata (es. il tipo di modifica da campo, valore vecchio e nuovo)",
}


def _target_label(r: dict):
    """Etichetta della proposta nella revisione (messaggio tradotto al momento di mostrarlo)."""
    v = lambda k: r.get(k) or "—"  # noqa: E731
    el = r["ocel_element"]
    if el == "object_type.key":
        return msg("Chiave oggetto → {o}", o=v("object_type"))
    if el == "object_type.attribute":
        return msg("Attributo oggetto → {o}.{a}", o=v("object_type"), a=v("attribute_name"))
    if el == "event_type.timestamp":
        return msg("Data evento → «{e}»", e=v("event_type"))
    if el == "event_type.time":
        return msg("Ora evento → «{e}» (unita alla data dell'evento)", e=v("event_type"))
    if el == "event_type.activity":
        values = r.get("activity_values")
        if not values:
            return msg("Attività da colonna → «{e}»: ogni valore è già il nome dell'attività", e=v("event_type"))
        present = r.get("present_values")
        items = [(k, values.get(k)) for k in present] if present is not None else list(values.items())
        shown = [f"{k} → {n}" if n else (msg("{k} → (escluso)", k=k) if n is not None else msg("{k} → (senza nome)", k=k))
                 for k, n in items[:6]]
        more = f" (+{len(items) - 6})" if len(items) > 6 else ""
        return msg("Attività da colonna → «{e}»: {v}{m}", e=v("event_type"), v=joined(shown), m=more)
    if el == "object_type.split":
        values = r.get("activity_values") or {}
        present = r.get("present_values")
        items = [(k, values.get(k)) for k in present] if present is not None else list(values.items())
        shown = [f"{k} → {n}" if n else (msg("{k} → (escluso)", k=k) if n is not None else msg("{k} → (resta {o})", k=k, o=v("object_type")))
                 for k, n in items[:6]]
        more = f" (+{len(items) - 6})" if len(items) > 6 else ""
        return msg("Divide {o} per valore: {v}{m}", o=v("object_type"), v=joined(shown), m=more)
    if el == COMPUTED and (r.get("activity_values") or {}).get("rule") == "slice":
        spec = r["activity_values"]
        a = int(spec.get("start") or 0) + 1
        return msg("Colonna calcolata {c}: posizioni {a}–{b} di {s}", c=v("source_column"), a=a,
                   b=a + int(spec.get("length") or 0) - 1, s=spec.get("column"))
    if el == COMPUTED:
        spec = r.get("activity_values") or {}
        return msg("Colonna calcolata {c}: Imposta / Rimuovi / Modifica + campo {f} (da {o} → {n})", c=v("source_column"),
                   f=spec.get("field") or "—", o=spec.get("old") or "—", n=spec.get("new") or "—")
    if el == "event_type.attribute":
        return msg("Attributo evento → «{e}».{a}", e=v("event_type"), a=v("attribute_name"))
    if el == "e2o_relationship":
        return msg("Relazione evento→oggetto → «{e}» —[{q}]→ {o}", e=v("event_type"), q=v("qualifier"),
                   o=v("related_object_type"))
    return el


@router.get("/ingestion/upload", response_class=HTMLResponse)
def upload_page(request: Request, workspace_id: str, edit_config_id: str | None = None):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    # None resetta la modalita' modifica se si riparte da zero (link "Apri Modulo 1");
    # valorizzato solo arrivando da "Modifica struttura" nel registro delle strutture.
    sess["edit_config_id"] = edit_config_id
    editing_config = None
    if edit_config_id:
        db = SessionLocal()
        try:
            editing_config = db.get(IngestionConfig, edit_config_id)
        finally:
            db.close()
    return templates.TemplateResponse(
        "upload.html", {
            "request": request, "user": user, "workspace_id": workspace_id, "context": sess["context"],
            "editing_config": editing_config, "step": 2,
            "assessment": assessment_status(workspace_id),
        }
    )


def _without_empty_columns(tables_schema: list) -> list:
    """Schema senza le colonne sempre vuote: non possono diventare eventi, chiavi o
    collegamenti, quindi non si mandano all'AI (meno token, meno righe da rivedere).
    Restano segnalate nel profilo dei dati."""
    return [replace(t, columns=[c for c in t.columns if c.null_ratio < 1.0]) for t in tables_schema]


def _find_identical_structure(workspace_id: str, fingerprint: dict[str, list[str]]) -> IngestionConfig | None:
    """Struttura in uso per l'analisi di questo processo con esattamente le stesse
    tabelle e colonne del caricamento (ordine delle colonne ininfluente)."""
    db = SessionLocal()
    try:
        ids = workspace_config_ids(db, workspace_id)
        if not ids:
            return None
        for config in (
            db.query(IngestionConfig)
            .filter(IngestionConfig.id.in_(ids), IngestionConfig.status == "approved")
            .order_by(IngestionConfig.created_at.desc())
            .all()
        ):
            existing = {t: sorted(cols) for t, cols in (config.schema_fingerprint or {}).items()}
            if existing and existing == fingerprint:
                return config
        return None
    finally:
        db.close()


@router.post("/ingestion/structures/{config_id}/update-from-upload")
def update_from_pending_upload(request: Request, config_id: str, workspace_id: str = Form(...), mode: str = Form("replace")):
    """Dalla pagina del doppione: usa i file appena caricati per aggiornare la
    struttura esistente, senza doverli ricaricare."""
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    paths = [Path(p) for p in sess.pop("pending_upload_paths", [])]
    if not paths:
        return RedirectResponse(f"/ingestion/structures/{config_id}/update-data?workspace_id={workspace_id}", status_code=303)
    return _apply_update(request, user, workspace_id, config_id, paths, mode)


@router.post("/ingestion/upload")
async def handle_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    workspace_id: str = Form(...),
    files: list[UploadFile] = File(default_factory=list),
    force_new: bool = Form(False),
):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    # l'assessment puo' essere cambiato da quando lo stato e' stato creato: si
    # rilegge a ogni upload, che e' il punto da cui partono pertinenza e mapping AI
    sess["context"] = mapping_context(workspace_id)

    if force_new and sess.get("pending_upload_paths"):
        # "Crea comunque una nuova struttura" dalla pagina del doppione: stessi file gia' caricati
        file_paths = [Path(p) for p in sess.pop("pending_upload_paths")]
    else:
        file_paths = _save_uploaded_files(files, UPLOAD_DIR / workspace_id)

    if not file_paths:
        editing_config = None
        edit_config_id = sess.get("edit_config_id")
        if edit_config_id:
            db = SessionLocal()
            try:
                editing_config = db.get(IngestionConfig, edit_config_id)
            finally:
                db.close()
        return templates.TemplateResponse(
            "upload.html", {
                "request": request, "user": user, "workspace_id": workspace_id, "context": sess["context"],
                "editing_config": editing_config, "step": 2,
                "assessment": assessment_status(workspace_id),
                "error": "Carica almeno un file CSV/TXT (o uno ZIP che li contenga) prima di continuare.",
            },
            status_code=400,
        )
    dataset_label = msg("{n} file caricati", n=len(file_paths))

    connector = FileConnector(file_paths)
    tables_schema = connector.discover_schema()
    tables_data = {t.name: connector.extract_full(t.name) for t in tables_schema}

    sess["dataset_label"] = dataset_label
    sess["tables_schema"] = [asdict(t) for t in tables_schema]
    # Oggetti live (non solo i dict serializzati sopra, che servono altrove es.
    # schema_fingerprint): servono intatti al passo successivo (descrizione
    # tabelle) e da li' alla chiamata AI Mapping, senza doverli ricostruire.
    sess["tables_schema_objs"] = tables_schema
    sess["tables_data"] = tables_data
    sess["mapping_rows"] = None
    sess["mapping_status"] = None
    sess["mapping_error"] = None
    sess["table_descriptions"] = {}
    sess["result"] = None
    sess["draft_config_id"] = None  # nuovi dati: il prossimo dataset e' nuovo

    # Struttura gia' esistente con le stesse identiche tabelle/colonne: nessun senso
    # rifare controllo di pertinenza + mapping AI (token sprecati e una struttura
    # doppione). Ci si ferma e si propone di aggiornare quella esistente. Non in
    # modalita' "Modifica struttura", dove rimappare gli stessi dati e' voluto.
    if not sess.get("edit_config_id") and not force_new:
        duplicate = _find_identical_structure(workspace_id, _schema_fingerprint(sess["tables_schema"]))
        if duplicate is not None:
            sess["pending_upload_paths"] = [str(p) for p in file_paths]
            return templates.TemplateResponse(
                "duplicate_structure.html", {
                    "request": request, "user": user, "workspace_id": workspace_id, "context": sess["context"],
                    "config": duplicate, "tables": tables_schema, "step": 2,
                }
            )

    # Profilazione deterministica (zero token) prima di qualunque chiamata AI:
    # chiavi, duplicati, collegamenti tra tabelle, qualita' delle date.
    answers, _ = load_assessment(workspace_id)
    date_cols = {t.name: [c.name for c in t.columns if c.inferred_type == "date"] for t in tables_schema}
    time_pairs = {t.name: {c.name: c.time_column for c in t.columns if c.time_column} for t in tables_schema}
    planned = {t.name: {c.name: c.planned_reason for c in t.columns if c.planned_reason} for t in tables_schema}
    # obiettivi dell'assessment che hanno bisogno di una scadenza: il profilo lo dice sulle date previste
    due_objectives = [o for o in answers.get("objectives") or []
                      if "due_date" in coverage.OBJECTIVE_NEEDS.get(o, {}).get("needs", [])
                      + coverage.OBJECTIVE_NEEDS.get(o, {}).get("useful", [])]
    sess["profile"] = await run_in_threadpool(
        profile_tables, tables_data, date_cols, (answers.get("period_from"), answers.get("period_to")),
        None, time_pairs, planned, due_objectives,
    )
    sess["relevance"] = None
    return RedirectResponse(url=f"/ingestion/profile?workspace_id={workspace_id}", status_code=303)


@router.get("/ingestion/profile", response_class=HTMLResponse)
def profile_page(request: Request, workspace_id: str):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    if not sess.get("profile"):
        return RedirectResponse(url=f"/ingestion/upload?workspace_id={workspace_id}", status_code=303)
    return templates.TemplateResponse(
        "profile.html", {
            "request": request, "user": user, "workspace_id": workspace_id, "context": sess["context"],
            "profile": sess["profile"], "dataset_label": sess.get("dataset_label"), "step": 2,
            **_coverage_context(request, workspace_id, sess),
        }
    )


def _coverage_context(request: Request, workspace_id: str, sess: dict) -> dict:
    """Copertura degli obiettivi per la pagina del profilo: livello senza costi, ultima valutazione
    con Claude per queste stesse tabelle (se c'e') e costo indicativo di una nuova valutazione."""
    # le tabelle che ripetono un'altra e verranno escluse non contano: le loro colonne sono gia' altrove
    skip = {c["table"] for c in (sess.get("profile") or {}).get("copies", []) if c["exclude"]}
    tables = [t for t in sess.get("tables_schema_objs") or [] if t.name not in skip]
    answers, _ = load_assessment(workspace_id)
    quick = coverage.quick_coverage(answers, tables)
    signature = coverage.tables_signature(tables)
    db = SessionLocal()
    try:
        stored = (db.query(ObjectiveCoverage).filter_by(workspace_id=workspace_id, tables_signature=signature)
                  .order_by(ObjectiveCoverage.created_at.desc()).first())
        stored = {"result": stored.result, "cost_usd": stored.cost_usd, "language": stored.language,
                  "created_at": stored.created_at.strftime("%Y-%m-%d %H:%M")} if stored else None
    finally:
        db.close()
    claude_ok = AI_MAPPER == "claude" and explain.available()
    estimate = None
    if claude_ok and (answers.get("key_questions") or answers.get("objectives") or answers.get("kpis")):
        estimate = coverage.estimate(coverage.build_payload(
            language=get_lang(request), process_name=sess["context"].get("process_name", ""), answers=answers,
            tables=tables, found=quick["found"]))
    return {"coverage": quick, "coverage_stored": stored, "coverage_estimate": estimate, "coverage_claude": claude_ok,
            "coverage_questions": answers.get("key_questions") or answers.get("kpis"),
            "coverage_has_objectives": bool(answers.get("objectives") or answers.get("key_questions"))}


@router.post("/ingestion/coverage")
async def coverage_evaluate(request: Request):
    """Valutazione con Claude della copertura degli obiettivi (costo indicativo mostrato prima)."""
    body = await request.json()
    workspace_id = str(body.get("workspace_id") or "")
    user, denied = _require_process_access(request, workspace_id)
    lang = get_lang(request)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    if AI_MAPPER != "claude" or not explain.available():
        return JSONResponse({"error": t(lang, "Claude non è configurato su questo server (manca la chiave API): la valutazione non è disponibile.")},
                            status_code=400)
    sess = _load_session(user.id, workspace_id)
    tables = sess.get("tables_schema_objs") or []
    if not tables:
        return JSONResponse({"error": t(lang, "Carica prima le tabelle.")}, status_code=400)
    answers, _ = load_assessment(workspace_id)
    found = coverage.quick_coverage(answers, tables)["found"]
    payload = coverage.build_payload(language=lang, process_name=sess["context"].get("process_name", ""),
                                     answers=answers, tables=tables, found=found)
    try:
        out = await run_in_threadpool(coverage.ask, payload)
    except Exception as exc:
        print(f"Copertura degli obiettivi non riuscita ({exc!r}).")
        return JSONResponse({"error": t(lang, "La richiesta a Claude non è riuscita: riprova tra poco.")}, status_code=502)
    db = SessionLocal()
    try:
        row = ObjectiveCoverage(workspace_id=workspace_id, tables_signature=coverage.tables_signature(tables),
                                language=lang, result=out["result"], cost_usd=out["cost_usd"], created_by=user.name)
        db.add(row)
        db.commit()
        created = row.created_at.strftime("%Y-%m-%d %H:%M")
    finally:
        db.close()
    return JSONResponse({"result": out["result"], "cost_usd": out["cost_usd"], "truncated": out["truncated"],
                         "created_at": created, "language": lang})


@router.post("/ingestion/profile/continue")
async def profile_continue(request: Request, workspace_id: str = Form(...)):
    """Dopo aver visto il profilo, l'utente decide di proseguire: solo ora parte
    il controllo di pertinenza (AI, economico), poi descrizione tabelle e mapping."""
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    tables_schema = sess.get("tables_schema_objs")
    if not tables_schema or not sess.get("profile"):
        return RedirectResponse(url=f"/ingestion/upload?workspace_id={workspace_id}", status_code=303)
    # tabelle che ripetono le righe di un'altra: escluse se l'utente lascia la casella spuntata
    form = await request.form()
    copies = sess["profile"].get("copies", [])
    excluded = {name for name in form.getlist("exclude") if name in {c["table"] for c in copies}}
    if excluded:
        tables_schema = [t for t in tables_schema if t.name not in excluded]
        sess["tables_schema_objs"] = tables_schema
        sess["tables_schema"] = [t for t in sess.get("tables_schema", []) if t["name"] not in excluded]
        sess["tables_data"] = {k: v for k, v in sess.get("tables_data", {}).items() if k not in excluded}
        sess["profile"]["copies"] = [c for c in copies if c["table"] not in excluded]
        sess["excluded_tables"] = sorted(excluded | set(sess.get("excluded_tables") or []))
    # le evidenze misurate (chiavi, date, collegamenti) arrivano all'AI Mapping con il contesto
    sess["context"]["data_profile"] = compact_for_mapping(sess["profile"])
    # le spiegazioni scritte da Claude (pertinenza, motivazioni del mapping) nella lingua dell'utente
    sess["context"]["language"] = get_lang(request)
    # Controllo di pertinenza prima del mapping costoso: solo con il mapper Claude
    # (con l'euristica mock non c'e' costo da evitare, ne' un modello che possa giudicare).
    verdict = None
    if AI_MAPPER == "claude":
        verdict = await run_in_threadpool(check_relevance, sess["context"], _without_empty_columns(tables_schema))
    sess["relevance"] = verdict.model_dump() if verdict else None
    return RedirectResponse(url=f"/ingestion/describe-tables?workspace_id={workspace_id}", status_code=303)


@router.get("/ingestion/describe-tables", response_class=HTMLResponse)
def describe_tables_page(request: Request, workspace_id: str):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    tables_schema = sess.get("tables_schema_objs")
    if not tables_schema:
        return RedirectResponse(url=f"/ingestion/upload?workspace_id={workspace_id}", status_code=303)
    descriptions = sess.get("table_descriptions", {})
    return templates.TemplateResponse(
        "describe_tables.html", {
            "request": request, "user": user, "workspace_id": workspace_id, "context": sess["context"],
            "tables": tables_schema, "descriptions": descriptions, "step": 2,
            "relevance": sess.get("relevance"), "error": None,
        }
    )


@router.post("/ingestion/describe-tables")
async def submit_table_descriptions(request: Request, background_tasks: BackgroundTasks, workspace_id: str = Form(...)):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    tables_schema = sess.get("tables_schema_objs")
    if not tables_schema:
        return RedirectResponse(url=f"/ingestion/upload?workspace_id={workspace_id}", status_code=303)

    form = await request.form()
    descriptions = {}
    for t in tables_schema:
        raw = form.get(f"desc_{t.name}")
        if raw and raw.strip():
            descriptions[t.name] = raw.strip()
    sess["table_descriptions"] = descriptions

    relevance = sess.get("relevance")
    if relevance and relevance["verdict"] == "non_coerente" and form.get("confirm_mismatch") != "1":
        return templates.TemplateResponse(
            "describe_tables.html", {
                "request": request, "user": user, "workspace_id": workspace_id, "context": sess["context"],
                "tables": tables_schema, "descriptions": descriptions, "step": 2, "relevance": relevance,
                "error": "Per procedere con questi dati conferma esplicitamente che sono quelli giusti.",
            },
            status_code=400,
        )

    sess["context"]["language"] = get_lang(request)
    # brani dei documenti di supporto che citano queste tabelle (scelti senza AI)
    mappable = _without_empty_columns(tables_schema)
    activity_cols = (sess["context"].get("data_profile") or {}).get("activity_columns", {})
    excerpts = await run_in_threadpool(select_excerpts, documents_for_mapping(workspace_id), [
        {"name": t.name, "columns": [c.name for c in t.columns],
         "activity_values": [v for vals in activity_cols.get(t.name, {}).values() for v in vals]}
        for t in mappable
    ])
    if excerpts["by_table"]:
        sess["context"]["document_excerpts"] = excerpts["by_table"]
    else:
        sess["context"].pop("document_excerpts", None)
    sess["document_context"] = {"docs": excerpts["used_docs"], "excerpts": excerpts["excerpt_count"],
                                "tables": sorted(excerpts["by_table"])}
    # prima del mapping colonna per colonna: quali oggetti di business entrano nel dataset
    return RedirectResponse(url=f"/ingestion/business-objects?workspace_id={workspace_id}", status_code=303)


def _start_mapping(sess: dict, background_tasks: BackgroundTasks, workspace_id: str) -> RedirectResponse:
    sess["mapping_rows"] = None
    sess["mapping_status"] = "pending"
    sess["mapping_error"] = None
    sess["mapping_started"] = time.time()
    sess["mapping_progress"] = None
    background_tasks.add_task(
        _run_ai_mapping, sess, _without_empty_columns(sess["tables_schema_objs"]), sess["tables_data"],
        sess["dataset_label"], sess.get("table_descriptions", {}),
    )
    return RedirectResponse(url=f"/ingestion/mapping-status?workspace_id={workspace_id}", status_code=303)


# ---------- oggetti di business (prima del mapping) ----------

def _bo_signature(sess: dict) -> str:
    return coverage.tables_signature(sess.get("tables_schema_objs") or [])


def _bo_proposal(sess: dict, workspace_id: str) -> dict:
    """Proposta corrente: quella di Claude o modificata dall'utente se c'e' per queste tabelle, altrimenti la bozza."""
    current = sess.get("bo_proposal")
    if current and current.get("signature") == _bo_signature(sess):
        return current
    answers, _ = load_assessment(workspace_id)
    current = business_objects.draft(sess["tables_schema_objs"], sess.get("profile") or {}, answers,
                                     (sess.get("context") or {}).get("language"))
    current["signature"] = _bo_signature(sess)
    sess["bo_proposal"] = current
    return current


def _bo_payload(request: Request, sess: dict, workspace_id: str) -> str:
    answers, _ = load_assessment(workspace_id)
    return business_objects.build_payload(
        language=get_lang(request), process_name=sess["context"].get("process_name", ""), answers=answers,
        tables=sess["tables_schema_objs"], data_profile=sess["context"].get("data_profile") or {},
        descriptions=sess.get("table_descriptions", {}))


@router.get("/ingestion/business-objects", response_class=HTMLResponse)
def business_objects_page(request: Request, workspace_id: str):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    if not sess.get("tables_schema_objs"):
        return RedirectResponse(url=f"/ingestion/upload?workspace_id={workspace_id}", status_code=303)
    proposal = _bo_proposal(sess, workspace_id)
    answers, _ = load_assessment(workspace_id)
    claude_ok = AI_MAPPER == "claude" and explain.available()
    profile_tables_ = {t["name"]: t for t in (sess.get("profile") or {}).get("tables", [])}
    return templates.TemplateResponse(
        "business_objects.html", {
            "request": request, "user": user, "workspace_id": workspace_id, "context": sess["context"], "step": 2,
            "proposal": proposal, "answers": answers, "claude_ok": claude_ok, "sess_error": sess.pop("bo_error", False),
            "estimate": business_objects.estimate(_bo_payload(request, sess, workspace_id)) if claude_ok else None,
            "tables": [{"name": tb.name, "columns": [c.name for c in tb.columns],
                        "key": (profile_tables_.get(tb.name) or {}).get("key") or []} for tb in sess["tables_schema_objs"]],
        }
    )


@router.post("/ingestion/business-objects/propose")
async def business_objects_propose(request: Request):
    """Proposta di Claude (costo indicativo mostrato prima nel pulsante)."""
    body = await request.json()
    workspace_id = str(body.get("workspace_id") or "")
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(get_lang(request), "Accesso negato.")}, status_code=403)
    sess = _load_session(user.id, workspace_id)
    if not sess.get("tables_schema_objs"):
        return JSONResponse({"error": t(get_lang(request), "Carica prima i dati.")}, status_code=400)
    if AI_MAPPER != "claude" or not explain.available():
        return JSONResponse({"error": t(get_lang(request), "Claude non è configurato su questo server.")}, status_code=400)
    try:
        out = await run_in_threadpool(business_objects.ask, _bo_payload(request, sess, workspace_id))
    except Exception as exc:  # rete, chiave, risposta non leggibile
        print(f"Oggetti di business: richiesta a Claude non riuscita ({exc!r})")
        return JSONResponse({"error": t(get_lang(request), "La richiesta a Claude non è riuscita: riprova tra poco.")}, status_code=502)
    result = out["result"]
    objects = business_objects.normalize(result["objects"], sess["tables_schema_objs"])
    if not objects:
        return JSONResponse({"error": t(get_lang(request), "Claude non ha proposto oggetti utilizzabili: resta la bozza.")}, status_code=502)
    sess["bo_proposal"] = {"source": "claude", "objects": objects, "not_objects": result["not_objects"],
                           "summary": result["summary"], "cost_usd": out["cost_usd"], "truncated": out["truncated"],
                           "signature": _bo_signature(sess)}
    return JSONResponse({"ok": True, "cost_usd": out["cost_usd"]})


def _bo_from_form(form, proposal: dict) -> list[dict]:
    """Le scelte dell'utente (incluso, nome, ruolo; un solo oggetto guida) sulla proposta corrente."""
    lead = form.get("lead")
    objects = []
    for i, o in enumerate(proposal["objects"]):
        o = dict(o)
        o["include"] = form.get(f"include_{i}") == "1"
        name = (form.get(f"name_{i}") or "").strip()
        if name:
            o["name"] = name
        role = form.get(f"role_{i}")
        if role in ("needed", "context"):
            o["role"] = role
        if lead is not None:
            if str(i) == lead:
                o["role"], o["include"] = "lead", True
            elif o["role"] == "lead":
                o["role"] = "needed"
        objects.append(o)
    return objects


@router.post("/ingestion/business-objects")
async def business_objects_submit(request: Request, background_tasks: BackgroundTasks,
                                  workspace_id: str = Form(...), action: str = Form(...)):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    if not sess.get("tables_schema_objs"):
        return RedirectResponse(url=f"/ingestion/upload?workspace_id={workspace_id}", status_code=303)
    form = await request.form()
    proposal = _bo_proposal(sess, workspace_id)
    tables = sess["tables_schema_objs"]
    back = RedirectResponse(url=f"/ingestion/business-objects?workspace_id={workspace_id}", status_code=303)
    if action == "skip":
        sess["business_objects"] = None
        sess["context"].pop("business_objects", None)
        return _start_mapping(sess, background_tasks, workspace_id)
    objects = _bo_from_form(form, proposal)
    if action.startswith("split|"):
        i = int(action.split("|", 1)[1])
        column = form.get(f"split_col_{i}")
        if 0 <= i < len(objects) and column:
            data = sess.get("tables_data", {}).get(objects[i]["table"], [])
            values = sorted({str(r.get(column) or "").strip() for r in data} - {"", "nan"})
            if 2 <= len(values) <= 20:
                objects[i:i + 1] = business_objects.split_object(objects[i], column, values)
    elif action == "add":
        table = form.get("add_table")
        name = (form.get("add_name") or "").strip()
        tb = next((x for x in tables if x.name == table), None)
        if tb and name:
            key = next((x["key"] for x in (sess.get("profile") or {}).get("tables", []) if x["name"] == table), None) or []
            column = form.get("add_filter_col") or None
            values = [v.strip() for v in (form.get("add_filter_values") or "").split(",") if v.strip()]
            objects.append({"name": name, "table": table, "key": key,
                            "filter": {"column": column, "values": values} if column and values else None,
                            "role": "needed", "include": True, "objective": "",
                            "why": msg("Aggiunto da te.")})
    objects = business_objects.normalize(objects, tables)
    proposal = {**proposal, "objects": objects, "signature": _bo_signature(sess)}
    sess["bo_proposal"] = proposal
    if action != "confirm":
        return back
    confirmed = [o for o in objects if o["include"]]
    if not confirmed:
        sess["bo_error"] = True
        return back
    if not business_objects.lead(objects):
        confirmed[0]["role"] = "lead"
    sess["business_objects"] = objects
    sess["context"]["business_objects"] = business_objects.for_context(objects)
    return _start_mapping(sess, background_tasks, workspace_id)


def _run_ai_mapping(
    sess: dict, tables_schema: list, tables_data: dict, dataset_label: str, table_descriptions: dict[str, str]
) -> None:
    """Gira dopo che la risposta HTTP e' gia' stata inviata (FastAPI BackgroundTasks):
    scrive l'esito in `sess`, che /ingestion/mapping-status legge via polling."""
    try:
        mapper, mapper_label = _get_ai_mapper()
        try:
            if isinstance(mapper, ClaudeAIMapper):
                proposals = mapper.propose_mapping(
                    tables_schema, sess["context"], table_descriptions,
                    progress=lambda info: sess.__setitem__("mapping_progress", info),
                )
            else:
                proposals = mapper.propose_mapping(tables_schema, sess["context"], table_descriptions)
        except MappingBudgetError:
            raise  # dataset troppo grande per il tetto di spesa: si ferma prima di spendere
        except Exception as exc:
            if mapper_label == "euristica mock":
                raise
            # La chiamata Claude puo' fallire per motivi esterni (rete, chiave non
            # valida, rate limit): meglio un fallback trasparente sull'euristica
            # mock, con l'errore reale visibile nei log e in etichetta, che un 500.
            print(f"ClaudeAIMapper ha fallito ({exc!r}): fallback sull'euristica mock per questo upload.")
            mapper_label = "euristica mock (fallback: chiamata Claude fallita)"
            proposals = HeuristicAIMapper().propose_mapping(tables_schema, sess["context"], table_descriptions)

        # colonne calcolate dal profilo dei dati: log di modifiche (attivita' da campo e valori) e chiavi dentro
        # valori composti (collegamenti), qualunque mapper abbia lavorato
        proposals = derived_columns.apply(proposals, sess["context"].get("data_profile") or {},
                                          sess["context"].get("language") or "it")
        # nomi conosciuti (dizionario SAP, catalogo) nella lingua dell'utente, qualunque mapper abbia lavorato
        proposals = names_i18n.localize_proposals(proposals, sess["context"].get("language") or "it")
        rejected: set[int] = set()
        if sess.get("business_objects"):
            # il mapping si allinea agli oggetti di business confermati (nomi, divisioni, oggetti esclusi)
            proposals, rejected = business_objects.apply(
                proposals, sess["business_objects"], tables_data,
                other_label=t(sess["context"].get("language") or "it", "altro"))
        rows = []
        for i, p in enumerate(proposals):
            d = asdict(p)
            d["row_id"] = i
            # Tutto parte come "proposed": e' il pulsante "Accetta tutte >= soglia" a promuovere
            # le righe ad alta confidence a "confirmed" in un click, esplicitamente. Pre-confermarle
            # gia' qui renderebbe quel pulsante un no-op silenzioso (bug reale trovato in test).
            # (gia' rifiutate: le proposte di oggetti che non sono tra gli oggetti di business confermati)
            d["status"] = "rejected" if i in rejected else "proposed"
            # snapshot immutabile di cio' che l'AI ha proposto in origine: sopravvive a
            # eventuali correzioni manuali successive, per audit trail (FieldMapping.original_ai_proposal)
            d["original_ai_proposal"] = {
                "ocel_element": p.ocel_element, "object_type": p.object_type, "event_type": p.event_type,
                "attribute_name": p.attribute_name, "qualifier": p.qualifier,
                "related_object_type": p.related_object_type, "activity_values": p.activity_values,
                "confidence": p.confidence, "rationale": p.rationale,
            }
            rows.append(d)

        sess["dataset_label"] = msg("{d} · AI Mapping Service: {m}", d=dataset_label, m=mapper_label)
        sess["mapping_rows"] = rows
        sess["mapping_missing_tables"] = _tables_without_proposals(tables_schema, rows)
        sess["mapping_cost_usd"] = getattr(mapper, "spent_usd", None)
        sess["mapping_known_tables"] = getattr(mapper, "known_tables", [])
        sess["mapping_budget_skipped"] = getattr(mapper, "budget_skipped", [])
        sess["mapping_status"] = "done"
    except Exception as exc:
        print(f"Generazione mapping fallita del tutto ({exc!r}).")
        sess["mapping_status"] = "error"
        sess["mapping_error"] = getattr(exc, "message", None) or str(exc)


def _tables_without_proposals(tables_schema: list, rows: list[dict]) -> list[str]:
    covered = {r["source_table"] for r in rows}
    return [t.name for t in tables_schema if t.columns and t.name not in covered]


def _run_regenerate_missing(sess: dict, tables: list, table_descriptions: dict[str, str]) -> None:
    """Rigenera le proposte solo per le tabelle rimaste scoperte, riusando come
    vocabolario i tipi di oggetto/evento gia' presenti, e le aggiunge alla revisione."""
    try:
        mapper, _ = _get_ai_mapper()
        rows = sess["mapping_rows"]
        vocabulary = {
            "object_types": {r["object_type"]: r["source_table"] for r in rows
                             if r["ocel_element"] == "object_type.key" and r.get("object_type")},
            "event_types": sorted({r["event_type"] for r in rows if r.get("event_type")}),
        }
        if isinstance(mapper, ClaudeAIMapper):
            proposals = mapper.propose_mapping(
                tables, sess["context"], table_descriptions, vocabulary,
                progress=lambda info: sess.__setitem__("mapping_progress", info),
            )
        else:
            proposals = mapper.propose_mapping(tables, sess["context"], table_descriptions)
        names = {t.name for t in tables}
        prof = sess["context"].get("data_profile") or {}
        proposals = derived_columns.apply(proposals, {
            "change_logs": {k: v for k, v in (prof.get("change_logs") or {}).items() if k in names},
            "embedded_keys": [e for e in prof.get("embedded_keys") or [] if e["table"] in names]},
            sess["context"].get("language") or "it")
        proposals = names_i18n.localize_proposals(proposals, sess["context"].get("language") or "it")
        next_id = max((r["row_id"] for r in rows), default=-1) + 1
        for i, p in enumerate(proposals):
            d = asdict(p)
            d["row_id"] = next_id + i
            d["status"] = "proposed"
            d["original_ai_proposal"] = {
                "ocel_element": p.ocel_element, "object_type": p.object_type, "event_type": p.event_type,
                "attribute_name": p.attribute_name, "qualifier": p.qualifier,
                "related_object_type": p.related_object_type, "activity_values": p.activity_values,
                "confidence": p.confidence, "rationale": p.rationale,
            }
            rows.append(d)
        sess["mapping_missing_tables"] = _tables_without_proposals(tables, rows)
        if getattr(mapper, "spent_usd", None) is not None:
            sess["mapping_cost_usd"] = (sess.get("mapping_cost_usd") or 0) + mapper.spent_usd
            sess["mapping_budget_skipped"] = getattr(mapper, "budget_skipped", [])
    except Exception as exc:
        print(f"Rigenerazione proposte non riuscita ({exc!r}).")
    sess["mapping_status"] = "done"


@router.get("/ingestion/mapping-status", response_class=HTMLResponse)
def mapping_status_page(request: Request, workspace_id: str):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    status = sess.get("mapping_status")

    if status == "done":
        return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}", status_code=303)
    if status is None:
        # Non ancora avviato (es. link diretto senza passare da descrizione tabelle):
        # non c'e' nessun background task che lo portera' mai a "done".
        return RedirectResponse(url=f"/ingestion/describe-tables?workspace_id={workspace_id}", status_code=303)

    return templates.TemplateResponse(
        "mapping_status.html", {
            "request": request, "user": user, "workspace_id": workspace_id, "context": sess["context"],
            "error": sess.get("mapping_error") if status == "error" else None, "step": 2,
            "progress": sess.get("mapping_progress"),
            "elapsed_min": int((time.time() - sess.get("mapping_started", time.time())) // 60),
        }
    )


@router.get("/ingestion/review", response_class=HTMLResponse)
def review_page(request: Request, workspace_id: str, error: str | None = None):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    rows = sess.get("mapping_rows")
    if rows is None or sess.get("mapping_status") == "pending":
        # Non ancora pronto (o mai partito, es. link diretto, o rigenerazione in corso):
        # manda alla pagina di attesa, e' quella che sa cosa fare in ogni stato.
        return RedirectResponse(url=f"/ingestion/mapping-status?workspace_id={workspace_id}", status_code=303)
    planned = {(t.name, c.name): c.planned_reason for t in sess.get("tables_schema_objs") or [] for c in t.columns
               if getattr(c, "planned_reason", None)}
    data_view = with_computed(sess.get("tables_data", {}), rows)
    for r in rows:
        r["planned_reason"] = (planned.get((r["source_table"], r["source_column"]))
                               if r["ocel_element"] == "event_type.timestamp" else None)
        if r["ocel_element"] == "e2o_relationship" and not r.get("qualifier"):
            r["qualifier"] = qualifier_for(r.get("related_object_type"), r.get("rationale"))
        if r["ocel_element"] in ("event_type.activity", "object_type.split"):
            # valori presenti nei dati caricati: la tabella di traduzione (es. dal dizionario SAP)
            # puo' prevedere anche codici che in questa estrazione non compaiono (con le colonne calcolate)
            data = data_view.get(r["source_table"], [])
            r["present_values"] = sorted({str(x.get(r["source_column"]) or "").strip() for x in data} - {"", "nan"})
        r["target_label"] = _target_label(r)

    order = _process_order(sess, rows)
    by_table: dict[str, list[dict]] = {}
    def group_pos(r):
        g = _row_group(r)
        if g is None:
            return 1e9
        return (order["events"] if g[0] == "evt" else order["objects"]).get(g[1], 1e9)
    for r in sorted(rows, key=lambda r: (order["tables"].get(r["source_table"], 1e9), r["source_table"], group_pos(r))):
        by_table.setdefault(r["source_table"], []).append(r)
    # tabelle da aprire subito: quelle con proposte ancora da decidere a confidence bassa
    tables_to_check = {
        r["source_table"] for r in rows
        if r["status"] == "proposed" and r["confidence"] < AUTO_ACCEPT_CONFIDENCE_THRESHOLD
    }
    # per ogni tabella, i gruppi del modello che alimenta (link dal dettaglio alle card)
    groups_by_table: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        g = _row_group(r)
        if g and g not in groups_by_table.setdefault(r["source_table"], []):
            groups_by_table[r["source_table"]].append(g)

    pending_count = sum(1 for r in rows if r["status"] == "proposed")

    blocked_message = None
    if error == "pending" and pending_count > 0:
        blocked_message = msg(
            "Non ho generato il dataset: ci sono ancora {n} proposte senza una decisione esplicita (righe "
            "evidenziate in giallo qui sotto). Accettale, rifiutale o modificale prima di confermare — oppure usa "
            "«Accetta le proposte ≥ soglia» per sbrigare in blocco quelle ad alta confidence.", n=pending_count)

    return templates.TemplateResponse(
        "mapping_review.html",
        {
            "request": request,
            "user": user,
            "workspace_id": workspace_id,
            "context": sess["context"],
            "dataset_label": sess["dataset_label"],
            "mapping_cost_usd": sess.get("mapping_cost_usd"), "mapping_budget_usd": AI_MAPPING_BUDGET_USD,
            "mapping_known_tables": sess.get("mapping_known_tables") or [],
            "document_context": _document_context(sess),
            "mapping_budget_skipped": sess.get("mapping_budget_skipped") or [],
            "template_labels": TEMPLATE_LABELS,
            "by_table": by_table,
            "tables_to_check": tables_to_check,
            "groups_by_table": groups_by_table,
            "missing_tables": _tables_without_proposals(_without_empty_columns(sess.get("tables_schema_objs") or []), rows),
            "model": _model_summary(rows, order),
            "link_options": _link_options(sess, rows),
            "regenerating": bool(sess.get("draft_config_id")),
            "pending_count": pending_count,
            "threshold": AUTO_ACCEPT_CONFIDENCE_THRESHOLD,
            "ocel_elements": VALID_OCEL_ELEMENTS,
            "element_labels": ELEMENT_LABELS,
            "fields_by_element": FIELDS_BY_ELEMENT,
            "known_objects": sorted({r["object_type"] for r in rows
                                     if r["ocel_element"] == "object_type.key" and r.get("object_type")}),
            "known_events": sorted({r["event_type"] for r in rows
                                    if r["ocel_element"] == "event_type.timestamp" and r.get("event_type")}),
            "blocked_message": blocked_message,
            "step": 3,
        },
    )


def _link_options(sess: dict, rows: list[dict]) -> dict[str, dict[str, list[str]]]:
    """Per «Aggiungi collegamento»: {evento: {oggetto: colonne in comune tra la tabella
    dell'evento e quella dell'oggetto}}. Solo oggetti di un'altra tabella con almeno
    una colonna in comune (e' la colonna su cui si cercano le righe da collegare)."""
    # prima le colonne piu' selettive (il numero documento, non la societa' che vale sempre uguale)
    columns = {t.name: [c.name for c in sorted(t.columns, key=lambda c: -(c.distinct_ratio or 0))]
               for t in sess.get("tables_schema_objs") or []}
    live = [r for r in rows if r["status"] != "rejected"]
    event_table = {r["event_type"]: r["source_table"] for r in live
                   if r["ocel_element"] == "event_type.timestamp" and r.get("event_type")}
    object_table = {r["object_type"]: r["source_table"] for r in live
                    if r["ocel_element"] == "object_type.key" and r.get("object_type")}
    out: dict[str, dict[str, list[str]]] = {}
    for event, e_table in event_table.items():
        e_cols = {c.upper(): c for c in columns.get(e_table, [])}
        for obj, o_table in object_table.items():
            if o_table == e_table:
                continue  # l'oggetto della stessa tabella e' gia' collegato da solo
            common = [c for c in columns.get(o_table, []) if c.upper() in e_cols]
            if common:
                out.setdefault(event, {})[obj] = common
    return out


def _add_link(rows: list[dict], event: str, obj: str, column: str, options: dict) -> None:
    """Nuovo collegamento evento -> oggetto deciso dall'utente in revisione."""
    if column not in options.get(event, {}).get(obj, []):
        return
    o_table = next(r["source_table"] for r in rows
                   if r["ocel_element"] == "object_type.key" and r.get("object_type") == obj)
    for r in rows:
        if (r["ocel_element"] == "e2o_relationship" and r.get("event_type") == event
                and r.get("related_object_type") == obj):
            r["status"] = "confirmed"  # esisteva gia' (magari rifiutato): si riattiva
            return
    rows.append({
        "row_id": max((r["row_id"] for r in rows), default=-1) + 1,
        "source_table": o_table, "source_column": column, "ocel_element": "e2o_relationship",
        "object_type": None, "event_type": event, "attribute_name": None, "qualifier": default_qualifier(obj),
        "related_object_type": obj, "activity_values": None, "confidence": 1.0, "based_on_template": None,
        "rationale": msg("Collegamento aggiunto nella revisione: gli eventi «{e}» riguardano {o}, cercando in {t} "
                         "le righe con lo stesso valore di {c}.", e=event, o=obj, t=o_table, c=column),
        "status": "overridden", "original_ai_proposal": None,
    })


def _link_any_column(sess: dict, rows: list[dict], event: str, obj: str, column: str) -> None:
    """Collegamento chiesto all'assistente: con una colonna in comune tra le due tabelle come «Aggiungi
    collegamento»; altrimenti con una colonna della tabella dell'evento che contiene il numero dell'oggetto
    (es. BSEG.AUGBL = numero dell'incasso), letta dal motore come chiave dell'oggetto collegato."""
    options = _link_options(sess, rows)
    if column in options.get(event, {}).get(obj, []):
        _add_link(rows, event, obj, column, options)
        return
    e_table = next((r["source_table"] for r in rows if r["ocel_element"] == "event_type.timestamp"
                    and r.get("event_type") == event and r["status"] != "rejected"), None)
    known_obj = any(r.get("object_type") == obj or obj in (r.get("activity_values") or {}).values()
                    for r in rows if r["ocel_element"] in ("object_type.key", "object_type.split") and r["status"] != "rejected")
    columns = {c.name for t in sess.get("tables_schema_objs") or [] if t.name == e_table for c in t.columns}
    if not e_table or not known_obj or column not in columns:
        return
    rows.append({
        "row_id": max((r["row_id"] for r in rows), default=-1) + 1,
        "source_table": e_table, "source_column": column, "ocel_element": "e2o_relationship",
        "object_type": None, "event_type": event, "attribute_name": None, "qualifier": default_qualifier(obj),
        "related_object_type": obj, "activity_values": None, "confidence": 1.0, "based_on_template": None,
        "rationale": msg("Collegamento aggiunto con l'assistente: {c} contiene il numero di {o}.", c=f"{e_table}.{column}", o=obj),
        "status": "overridden", "original_ai_proposal": None,
    })


@router.post("/ingestion/review/assistant")
async def review_assistant_ask(request: Request):
    """Assistente della revisione: con estimate=true solo il costo indicativo; le modifiche che propone tornano
    come azioni da confermare (descritte), non vengono applicate qui."""
    body = await request.json()
    workspace_id = str(body.get("workspace_id") or "")
    lang = get_lang(request)
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    if AI_MAPPER != "claude" or not explain.available():
        return JSONResponse({"available": False, "error": t(
            lang, "Claude non è configurato su questo server (manca la chiave API): l'assistente non è disponibile.")})
    sess = _load_session(user.id, workspace_id)
    rows = sess.get("mapping_rows")
    if rows is None:
        return JSONResponse({"error": t(lang, "La revisione non è più disponibile: riaprila dall'elenco dei dataset.")}, status_code=409)
    answers, _ = load_assessment(workspace_id)
    order = _process_order(sess, rows)
    context = review_assistant.build_context(
        language=lang, process_name=sess["context"].get("process_name", ""), assessment=answers,
        rows=rows, model=_model_summary(rows, order), business_objects=sess.get("business_objects"),
        profile_issues=[f"{i['table'] or ''}: {render(lang, i['title'])}" for i in (sess.get("profile") or {}).get("issues", [])],
        tables=sess.get("tables_schema_objs") or [], link_options=_link_options(sess, rows),
        ui_labels=ui_labels(lang, review_assistant.UI_LABELS),
        facts=review_assistant.data_facts(sess.get("tables_schema_objs") or [], sess.get("tables_data") or {},
                                          sess.get("profile")))
    history = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    est = review_assistant.estimate(context, history)
    if body.get("estimate"):
        return JSONResponse({"available": True, **est})
    try:
        # dati in sola lettura, con le colonne calcolate del mapping (es. CHANGE_KIND)
        data = with_computed(sess.get("tables_data") or {}, rows)
        result = await run_in_threadpool(review_assistant.ask, context, history, data)
    except Exception as exc:
        print(f"Assistente della revisione non riuscito ({exc!r}).")
        return JSONResponse({"available": True, "error": t(lang, "La richiesta a Claude non è riuscita: riprova tra poco.")},
                            status_code=502)
    actions = []
    columns = {tb.name: [c.name for c in tb.columns] for tb in sess.get("tables_schema_objs") or []}
    for a in result.get("actions", []):
        d = review_assistant.describe(a, rows, columns)
        if d:
            actions.append({"action": d["action"], "lines": [render(lang, x) for x in d["lines"]], "why": d["why"]})
    answer = result.get("answer") or ""
    if result.get("truncated"):
        answer = answer.rstrip() + "…\n\n" + t(lang, "(Risposta interrotta perché troppo lunga: fai una domanda più precisa per avere il resto.)")
    return JSONResponse({"available": True, "answer": answer, "actions": actions, "cost_usd": result.get("cost_usd")})


@router.post("/ingestion/review/assistant/apply")
async def review_assistant_apply(request: Request):
    """Applica una modifica proposta dall'assistente e confermata dall'utente."""
    body = await request.json()
    workspace_id = str(body.get("workspace_id") or "")
    lang = get_lang(request)
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(lang, "Accesso negato.")}, status_code=403)
    sess = _load_session(user.id, workspace_id)
    rows = sess.get("mapping_rows")
    if rows is None:
        return JSONResponse({"error": t(lang, "La revisione non è più disponibile: riaprila dall'elenco dei dataset.")}, status_code=409)
    columns = {tb.name: [c.name for c in tb.columns] for tb in sess.get("tables_schema_objs") or []}
    d = review_assistant.describe(body.get("action") or {}, rows, columns)
    ok = bool(d) and review_assistant.apply(
        d["action"], rows, add_link=lambda rs, e, o, c: _link_any_column(sess, rs, e, o, c))
    if not ok:
        return JSONResponse({"error": t(lang, "Modifica non applicabile: il mapping è cambiato nel frattempo.")}, status_code=400)
    return JSONResponse({"ok": True})


def _document_context(sess: dict) -> dict | None:
    """Brani dei documenti arrivati davvero a Claude: solo con il mapper Claude e solo
    per le tabelle non riconosciute dalle regole (le altre non passano dall'AI)."""
    if sess.get("mapping_cost_usd") is None or "document_context" not in sess:
        return None
    known = set(sess.get("mapping_known_tables") or [])
    by_table = {t: v for t, v in (sess["context"].get("document_excerpts") or {}).items() if t not in known}
    return {"excerpts": sum(len(v) for v in by_table.values()), "tables": sorted(by_table),
            "docs": sorted({e["doc"] for v in by_table.values() for e in v})}


def _row_group(r: dict) -> tuple[str, str] | None:
    """Gruppo del modello a cui appartiene una riga di mapping: un tipo di oggetto
    (chiave e attributi) o un tipo di evento (timestamp, attributi, collegamenti)."""
    el = r["ocel_element"]
    if el.startswith("object_type") and r.get("object_type"):
        return ("obj", r["object_type"])
    if (el.startswith("event_type") or el == "e2o_relationship") and r.get("event_type"):
        return ("evt", r["event_type"])
    return None


def _process_order(sess: dict, rows: list[dict]) -> dict:
    """Ordine di processo per la revisione: eventi per istante mediano (cosi' come
    avvengono), oggetti per il primo evento che li coinvolge, tabelle per il primo
    oggetto/evento che alimentano. Calcolato sui dati con il mapping corrente e
    tenuto in cache finche' il mapping non cambia. Usa anche le righe rifiutate, cosi'
    rifiutare un gruppo non lo sposta: l'ordine resta stabile mentre si decide."""
    used = rows
    signature = _rows_signature(used, None)
    cached = sess.get("process_order")
    if cached and cached["signature"] == signature:
        return cached["order"]
    events_order: dict[str, float] = {}
    objects_order: dict[str, float] = {}
    try:
        ocel, _, stats = build_ocel(sess["tables_data"], used)
        # le attivita' lette da una colonna contano per il tipo di evento del mapping che le genera
        group_of = {a: g for g, produced in stats["activities"].items() for a in produced}
        times: dict[str, list[str]] = {}
        for e in ocel["events"]:
            times.setdefault(group_of.get(e["type"], e["type"]), []).append(e["time"])
        ranked = sorted(times, key=lambda t: sorted(times[t])[len(times[t]) // 2])
        events_order = {t: i for i, t in enumerate(ranked)}
        # con la divisione per valore gli oggetti hanno il tipo della divisione: contano per il tipo del mapping
        base_of = stats.get("object_subtypes") or {}
        obj_type = {o["id"]: base_of.get(o["type"], o["type"]) for o in ocel["objects"]}
        linked_events: dict[str, set] = {}
        for e in ocel["events"]:
            for r in e["relationships"]:
                t = obj_type.get(r["objectId"])
                if t is not None:
                    group = group_of.get(e["type"], e["type"])
                    objects_order[t] = min(objects_order.get(t, 1e9), events_order[group])
                    linked_events.setdefault(t, set()).add(group)
        # a parita' di primo evento viene prima l'oggetto collegato a piu' attivita' (il piu' centrale)
        for t in objects_order:
            objects_order[t] += 0.5 - len(linked_events.get(t, ())) / (2 * max(len(events_order), 1) + 1)
    except Exception as exc:  # l'ordine e' un aiuto alla lettura: mai bloccare la revisione
        print(f"Ordine di processo non calcolabile ({exc!r}): uso l'ordine alfabetico.")
    tables_order: dict[str, float] = {}
    for r in used:
        g = _row_group(r)
        if g is None:
            continue
        if g[0] == "evt":  # conta la tabella da cui nasce l'evento (quella della sua data)
            pos = events_order.get(g[1]) if r["ocel_element"] == "event_type.timestamp" else None
        else:
            pos = objects_order.get(g[1])
        if pos is not None:
            tables_order[r["source_table"]] = min(tables_order.get(r["source_table"], 1e9), pos)
    order = {"events": events_order, "objects": objects_order, "tables": tables_order}
    sess["process_order"] = {"signature": signature, "order": order}
    return order


def _model_summary(rows: list[dict], order: dict | None = None) -> dict:
    """Il modello proposto visto dall'alto: per ogni tipo di oggetto/evento le sue
    righe di mapping, cosi' l'utente decide per gruppo invece che colonna per colonna."""
    groups: dict[tuple[str, str], dict] = {}
    for r in rows:
        g = _row_group(r)
        if g is None:
            continue
        d = groups.setdefault(g, {
            "kind": g[0], "name": g[1], "tables": [], "keys": [], "timestamps": [], "links": [],
            "attributes": 0, "total": 0, "pending": 0, "low": 0, "rejected": 0, "min_conf": 1.0,
        })
        if r["source_table"] not in d["tables"]:
            d["tables"].append(r["source_table"])
        d["total"] += 1
        d["min_conf"] = min(d["min_conf"], r["confidence"])
        if r["status"] == "rejected":
            d["rejected"] += 1
        elif r["status"] == "proposed":
            d["pending"] += 1
            if r["confidence"] < AUTO_ACCEPT_CONFIDENCE_THRESHOLD:
                d["low"] += 1
        # chiavi/date/collegamenti: quelli in vigore (righe non rifiutate); per un gruppo
        # rifiutato per intero si mostrano quelli proposti, per poterlo valutare e ripristinare
        el = r["ocel_element"]
        for bucket in (("all_",) if r["status"] == "rejected" else ("", "all_")):
            if el == "object_type.key":
                d.setdefault(bucket + "keys", []).append(r["source_column"])
            elif el == "event_type.timestamp":
                d.setdefault(bucket + "timestamps", []).append(f"{r['source_table']}.{r['source_column']}")
            elif el == "event_type.time":
                d.setdefault(bucket + "times", []).append(r["source_column"])
            if el == "event_type.timestamp" and r.get("planned_reason"):
                d[bucket + "planned"] = msg("{c}: {r}", c=r["source_column"], r=r["planned_reason"])
            elif el == "event_type.activity":
                values = r.get("activity_values")
                present = r.get("present_values")
                if values and present is not None:
                    values = {k: values.get(k) for k in present}
                d[bucket + "activity"] = {
                    "column": r["source_column"],
                    "names": sorted({n for n in values.values() if n}) if values else None,
                    "excluded": sorted(k for k, n in values.items() if n == "") if values else [],
                    "unnamed": sorted(k for k, n in values.items() if n is None) if values else [],
                }
            elif el == "object_type.split":
                values = r.get("activity_values") or {}
                present = r.get("present_values")
                if present is not None:
                    values = {k: values.get(k) for k in present}
                d[bucket + "split"] = {
                    "column": r["source_column"],
                    "names": sorted({n for n in values.values() if n}),
                    "excluded": sorted(k for k, n in values.items() if n == ""),
                    "unnamed": sorted(k for k, n in values.items() if n is None),
                }
            elif el == "e2o_relationship" and r.get("related_object_type"):
                links = d.setdefault(bucket + "links", [])
                if r["related_object_type"] not in links:
                    links.append(r["related_object_type"])
        if el not in ("object_type.key", "object_type.split", "event_type.timestamp", "event_type.time",
                      "event_type.activity", "e2o_relationship"):
            d["all_attributes"] = d.get("all_attributes", 0) + 1
            if r["status"] != "rejected":
                d["attributes"] += 1
    for d in groups.values():
        if d["rejected"] == d["total"]:
            for k in ("keys", "timestamps", "times", "links"):
                d[k] = d.get("all_" + k, [])
            d["planned"] = d.get("all_planned")
            d["activity"] = d.get("all_activity")
            d["split"] = d.get("all_split")
            d["attributes"] = d.get("all_attributes", 0)
    order = order or {"events": {}, "objects": {}}
    for g in groups.values():
        g["anchor"] = f"grp-{g['kind']}-{g['name'].replace(' ', '_')}"
        g["accepted"] = g["total"] - g["pending"] - g["rejected"]

    def key(g, positions):  # ordine di processo, stabile: un gruppo rifiutato resta al suo posto
        return (positions.get(g["name"], 1e9), g["name"])
    objects = sorted((g for g in groups.values() if g["kind"] == "obj"), key=lambda g: key(g, order["objects"]))
    events = sorted((g for g in groups.values() if g["kind"] == "evt"), key=lambda g: key(g, order["events"]))
    return {"objects": objects, "events": events}


@router.post("/ingestion/review")
async def submit_review(
    request: Request, background_tasks: BackgroundTasks, workspace_id: str = Form(...), action: str = Form(...)
):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    rows = sess["mapping_rows"]
    form = await request.form()

    for r in rows:
        decision = form.get(f"decision_{r['row_id']}")
        if decision in ("confirmed", "rejected"):
            r["status"] = decision

        changed = False
        for field in EDITABLE_FIELDS:
            submitted = form.get(f"field_{field}_{r['row_id']}")
            if submitted is None:
                continue
            submitted = submitted.strip() or None
            if submitted != r.get(field):
                r[field] = submitted
                changed = True
        submitted = form.get(f"field_activity_values_{r['row_id']}")
        if submitted is not None and r["ocel_element"] in ("event_type.activity", "object_type.split"):
            values = parse_activity_values(submitted)
            if values != (r.get("activity_values") or None):
                r["activity_values"] = values
                changed = True
        if changed:
            normalize_row(r)
        if r["ocel_element"] == "object_type.split" and not r.get("activity_values") and r.get("source_column"):
            # divisione appena scelta: si parte dai valori presenti, tutti ancora nell'oggetto del mapping;
            # l'utente scrive accanto a ognuno il tipo di oggetto (o lo lascia vuoto per escluderlo)
            data = sess.get("tables_data", {}).get(r["source_table"], [])
            present = sorted({str(x.get(r["source_column"]) or "").strip() for x in data} - {"", "nan"})
            r["activity_values"] = {v: r.get("object_type") or "" for v in present} or None
            changed = True
        # una correzione manuale prevale sulla decisione radio: la riga resta
        # "nel mapping" ma tracciata come intervento umano, non proposta AI accettata
        if changed and r["status"] != "rejected":
            r["status"] = "overridden"

    if action == "bulk_accept":
        for r in rows:
            if r["status"] == "proposed" and r["confidence"] >= AUTO_ACCEPT_CONFIDENCE_THRESHOLD:
                r["status"] = "confirmed"
        return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}", status_code=303)

    if action == "bulk_accept_all":
        # Ignora la soglia di confidence: accetta ogni proposta ancora senza
        # decisione, comprese quelle a bassa confidence dell'euristica generica.
        # Da usare quando l'utente ha già verificato le rationale e vuole
        # sbrigare in blocco (es. su tabelle non nel catalogo ma comunque note
        # a chi rivede, come tabelle SAP standard).
        for r in rows:
            if r["status"] == "proposed":
                r["status"] = "confirmed"
        return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}", status_code=303)

    if action == "save":
        back = form.get("return_to") or ""
        anchor = f"#{back}" if back.replace("-", "").isalnum() else ""
        return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}{anchor}", status_code=303)

    # decisione per gruppo del modello: "group_accept|obj|PurchaseOrder". Si puo' sempre
    # cambiare idea: accettare ripristina anche le righe rifiutate (le modifiche manuali restano).
    if action.startswith("add_link|"):
        event = action.split("|", 1)[1]
        anchor = f"grp-evt-{event.replace(' ', '_')}"
        _add_link(rows, event, (form.get(f"link_obj_{anchor}") or "").strip(),
                  (form.get(f"link_col_{anchor}") or "").strip(), _link_options(sess, rows))
        return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}#{anchor}", status_code=303)

    if action.startswith(("group_accept|", "group_reject|")):
        verb, kind, name = action.split("|", 2)
        for r in rows:
            if _row_group(r) != (kind, name):
                continue
            if verb == "group_reject":
                r["status"] = "rejected"
            elif r["status"] in ("proposed", "rejected"):
                r["status"] = "confirmed"
        anchor = f"grp-{kind}-{name.replace(' ', '_')}"
        return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}#{anchor}", status_code=303)

    if action == "regenerate_missing":
        schema = _without_empty_columns(sess["tables_schema_objs"])
        missing = set(_tables_without_proposals(schema, rows))
        tables = [t for t in schema if t.name in missing]
        if tables:
            sess["mapping_status"] = "pending"
            sess["mapping_started"] = time.time()
            sess["mapping_progress"] = None
            background_tasks.add_task(_run_regenerate_missing, sess, tables, sess.get("table_descriptions", {}))
            return RedirectResponse(url=f"/ingestion/mapping-status?workspace_id={workspace_id}", status_code=303)
        return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}", status_code=303)

    if action == "finalize":
        still_pending = [r for r in rows if r["status"] == "proposed"]
        if still_pending:
            return RedirectResponse(
                url=f"/ingestion/review?workspace_id={workspace_id}&error=pending", status_code=303
            )
        _finalize(workspace_id, sess, user)
        return RedirectResponse(url=f"/ingestion/result?workspace_id={workspace_id}", status_code=303)

    return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}", status_code=303)


def _rows_signature(rows: list[dict], case_type: str | None) -> str:
    """Impronta del mapping: l'ordine di processo in cache si ricalcola solo se cambia."""
    import hashlib
    key = json.dumps(
        [[r["source_table"], r["source_column"], r["ocel_element"], r.get("object_type"), r.get("event_type"),
          r.get("attribute_name"), r.get("related_object_type"), r.get("activity_values")] for r in rows] + [case_type],
        ensure_ascii=False,
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _main_object(workspace_id: str) -> str | None:
    answers, _ = load_assessment(workspace_id)
    return answers.get("main_object")


def _planned_events(confirmed: list[dict], tables_schema: list) -> dict[str, str]:
    """{tipo di evento: motivo} per gli eventi la cui data e' una data prevista o di scadenza."""
    planned = {(t.name, c.name): c.planned_reason for t in tables_schema for c in t.columns
               if getattr(c, "planned_reason", None)}
    return {r["event_type"]: msg("{c}, {r}", c=f"{r['source_table']}.{r['source_column']}",
                                 r=planned[(r["source_table"], r["source_column"])])
            for r in confirmed
            if r["ocel_element"] == "event_type.timestamp" and (r["source_table"], r["source_column"]) in planned}


def _finalize(workspace_id: str, sess: dict, user: User) -> None:
    rows = sess["mapping_rows"]
    confirmed = [r for r in rows if r["status"] in ("confirmed", "overridden")]

    ocel, skip_log, stats = build_ocel(sess["tables_data"], confirmed)
    dq_results = run_data_quality_checks(
        ocel, skip_log, stats, _main_object(workspace_id), _planned_events(confirmed, sess.get("tables_schema_objs") or []),
        business_objects=sess.get("business_objects"),
    )
    object_defs, event_defs = compile_defs(confirmed)
    schema_fp = _schema_fingerprint(sess["tables_schema"])
    edit_config_id = sess.get("edit_config_id")

    # Tornati alla revisione dal risultato e rigenerato: si aggiorna lo stesso dataset invece
    # di crearne un altro. Una bozza mai promossa si rifa' da capo; se nel frattempo e' stata
    # promossa diventa una nuova versione (come «Modifica mapping»).
    draft_id = sess.get("draft_config_id")
    same_draft = False
    if draft_id:
        db = SessionLocal()
        try:
            previous = db.get(IngestionConfig, draft_id)
            if previous is not None and previous.status == "draft" and sess.get("draft_is_new") and not edit_config_id:
                # bozza nata in questa sessione e mai promossa: non ha storia da conservare
                old_files = delete_structures(db, [draft_id])
                db.commit()
                remove_files(old_files)
            elif previous is not None:
                # dataset con una storia (versioni, dati gia' usati per l'analisi): nuova versione se
                # nel frattempo e' stato promosso, altrimenti si aggiorna la stessa versione in bozza
                same_draft = previous.status == "draft"
                edit_config_id = edit_config_id or draft_id
        finally:
            db.close()

    ocel_path = OUTPUT_DIR / f"{workspace_id}-{uuid.uuid4().hex[:8]}.ocel.json"
    ocel_path.write_text(json.dumps(ocel, indent=2, ensure_ascii=False), encoding="utf-8")

    db = SessionLocal()
    try:
        ctx = sess["context"]

        if edit_config_id:
            config = db.get(IngestionConfig, edit_config_id)
            config.schema_fingerprint = schema_fp
            if not same_draft:  # rigenerare piu' volte la stessa bozza non crea versioni in piu'
                config.current_version += 1
                db.add(IngestionConfigVersion(
                    ingestion_config_id=config.id, version=config.current_version,
                    changelog="Struttura rigenerata dal Data Engineer (nuovo mapping su nuovi dati).",
                    approved_by=user.name,
                ))
            # richiede una nuova promozione esplicita: non torna attiva per l'analisi da sola
            config.status = "draft"
            # sostituisce interamente le definizioni precedenti con quelle appena confermate
            db.query(ObjectTypeDef).filter_by(ingestion_config_id=config.id).delete()
            db.query(EventTypeDef).filter_by(ingestion_config_id=config.id).delete()
            db.query(FieldMapping).filter_by(ingestion_config_id=config.id).delete()
            db.flush()
        else:
            system_type = "GenericFile"
            source_system = db.query(SourceSystem).filter_by(system_type=system_type).first()
            if source_system is None:
                source_system = SourceSystem(name="File Upload (CSV/TXT)", system_type=system_type)
                db.add(source_system)
                db.flush()

            connector_row = db.query(ConnectorModel).filter_by(
                source_system_id=source_system.id, plugin_id="file_connector"
            ).first()
            if connector_row is None:
                connector_row = ConnectorModel(
                    source_system_id=source_system.id, plugin_id="file_connector",
                    supports_incremental=False,
                )
                db.add(connector_row)
                db.flush()

            config = IngestionConfig(
                name=f"{ctx['process_name']} - {source_system.name}",
                source_system_id=source_system.id,
                process_type=ctx.get("process_type", ""),
                status="draft",
                current_version=1,
                owner=user.name,
                schema_fingerprint=schema_fp,
            )
            db.add(config)
            db.flush()

            db.add(IngestionConfigVersion(
                ingestion_config_id=config.id, version=1,
                changelog="Prima versione confermata dal Data Engineer nel wizard di ingestion.",
                approved_by=user.name,
            ))

            db.add(ProcessIngestionLink(
                workspace_id=workspace_id, ingestion_config_id=config.id,
                pinned_version=1, linked_by=user.name, approved_by=user.name,
            ))

        for od in object_defs.values():
            db.add(ObjectTypeDef(
                ingestion_config_id=config.id, name=od.name,
                source_table=od.source_table, key_columns=",".join(od.key_columns),
            ))
        for ed in event_defs.values():
            db.add(EventTypeDef(
                ingestion_config_id=config.id, name=ed.name,
                source_table=ed.source_table, timestamp_column=ed.timestamp_column,
                activity_column=ed.activity_column,
            ))

        if sess.get("business_objects") is not None:
            # oggetti di business confermati prima del mapping (testi come JSON: si traducono quando si mostrano)
            config.business_objects = json.loads(json.dumps(sess["business_objects"], default=str))
        for r in rows:
            overridden = r["status"] == "overridden"
            db.add(FieldMapping(
                ingestion_config_id=config.id,
                source_table=r["source_table"], source_column=r["source_column"],
                ocel_element=r["ocel_element"], object_type=r["object_type"], event_type=r["event_type"],
                attribute_name=r["attribute_name"], qualifier=r["qualifier"],
                related_object_type=r["related_object_type"], activity_values=r.get("activity_values"),
                proposal_source="user" if overridden else "ai",
                confidence=r["confidence"], rationale=to_text(r["rationale"]),
                based_on_template=r["based_on_template"],
                original_ai_proposal=r["original_ai_proposal"] if overridden else None,
                status=r["status"], confirmed_by=user.name,
            ))

        run = ExtractionRun(
            workspace_id=workspace_id, ingestion_config_id=config.id,
            run_type="snapshot", status="completed",
            object_count=stats["object_count"], event_count=stats["event_count"],
            ocel_file_path=str(ocel_path),
        )
        db.add(run)
        db.flush()

        for dq in dq_results:
            db.add(DataQualityCheckResult(
                extraction_run_id=run.id, check_name=dq["check_name"], severity=dq["severity"],
                passed=dq["passed"], details=to_text(dq["details"]), affected_count=dq["affected_count"],
            ))

        db.commit()
        ingestion_config_id = config.id
    finally:
        db.close()

    sess["draft_config_id"] = ingestion_config_id
    sess["draft_is_new"] = not edit_config_id
    sess["result"] = {
        "ocel_path": str(ocel_path),
        "stats": stats,
        "dq_results": dq_results,
        "ingestion_config_id": ingestion_config_id,
        "rejected_count": sum(1 for r in rows if r["status"] == "rejected"),
        "overridden_count": sum(1 for r in rows if r["status"] == "overridden"),
    }


@router.post("/ingestion/explain")
async def explain_message(request: Request):
    """«Chiedi a Claude» su un messaggio. Con estimate=true non chiama Claude: restituisce
    solo il costo indicativo, che l'utente vede e conferma prima dell'invio vero."""
    body = await request.json()
    workspace_id = str(body.get("workspace_id") or "")
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return JSONResponse({"error": t(get_lang(request), "Accesso negato.")}, status_code=403)
    lang = get_lang(request)
    if AI_MAPPER != "claude" or not explain.available():
        return JSONResponse({"available": False, "error": t(
            lang, "Claude non è configurato su questo server (manca la chiave API): la spiegazione non è disponibile.")})
    sess = _load_session(user.id, workspace_id)
    table = next((tb for tb in sess.get("tables_schema_objs") or [] if tb.name == body.get("table")), None)
    payload = explain.build_payload(
        language=lang, page=str(body.get("page") or ""), message=str(body.get("message") or ""),
        question=str(body.get("question") or ""), context=sess.get("context") or {}, table=table,
        ui_labels=ui_labels(lang, explain.UI_LABELS),
    )
    est = explain.estimate(payload)
    if body.get("estimate"):
        return JSONResponse({"available": True, **est})
    try:
        result = await run_in_threadpool(explain.ask, payload)
    except Exception as exc:
        print(f"Chiedi a Claude non riuscito ({exc!r}).")
        return JSONResponse({"available": True, "error": t(lang, "La richiesta a Claude non è riuscita: riprova tra poco.")},
                            status_code=502)
    if result.get("truncated"):
        result["answer"] = result["answer"].rstrip() + "…\n\n" + t(
            lang, "(Risposta interrotta perché troppo lunga: fai una domanda più precisa per avere il resto.)")
    return JSONResponse({"available": True, **result})


@router.get("/ingestion/result", response_class=HTMLResponse)
def result_page(request: Request, workspace_id: str):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    result = sess.get("result")
    if not result:
        return RedirectResponse(url=f"/ingestion/upload?workspace_id={workspace_id}", status_code=303)

    db = SessionLocal()
    try:
        config = db.get(IngestionConfig, result["ingestion_config_id"])
        structure_status = config.status
    finally:
        db.close()

    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
            "user": user,
            "workspace_id": workspace_id,
            "context": sess["context"],
            "result": result,
            "structure_status": structure_status,
            "step": 5,
        },
    )


@router.post("/ingestion/structures/{config_id}/promote")
def promote_structure(request: Request, config_id: str, workspace_id: str = Form(...)):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied

    db = SessionLocal()
    try:
        config = db.get(IngestionConfig, config_id)
        config.status = "approved"
        link = db.query(ProcessIngestionLink).filter_by(
            workspace_id=workspace_id, ingestion_config_id=config_id
        ).first()
        if link is None:
            db.add(ProcessIngestionLink(
                workspace_id=workspace_id, ingestion_config_id=config_id,
                pinned_version=config.current_version, linked_by=user.name, approved_by=user.name,
            ))
        else:
            link.approved_by = user.name
        db.commit()
    finally:
        db.close()

    return RedirectResponse(f"/ingestion/structures?workspace_id={workspace_id}", status_code=303)


@router.post("/ingestion/structures/{config_id}/toggle-catalog")
def toggle_catalog(request: Request, config_id: str, workspace_id: str = Form(...)):
    """Aggiunge/rimuove questa struttura dal catalogo di pattern riusabili
    (services/catalog.dynamic_lookup): non e' un'entita' separata, solo un
    flag su IngestionConfig - eliminare la struttura elimina anche questo."""
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied

    db = SessionLocal()
    try:
        config = db.get(IngestionConfig, config_id)
        if config is None:
            return RedirectResponse(f"/ingestion/structures?workspace_id={workspace_id}", status_code=303)
        config.in_catalog = not config.in_catalog
        db.commit()
    finally:
        db.close()

    return RedirectResponse(f"/ingestion/structures?workspace_id={workspace_id}", status_code=303)


@router.get("/ingestion/structures", response_class=HTMLResponse)
def list_structures(request: Request, workspace_id: str):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied

    db = SessionLocal()
    try:
        ws = db.get(ProcessWorkspace, workspace_id)
        configs = (
            db.query(IngestionConfig)
            .join(ExtractionRun, ExtractionRun.ingestion_config_id == IngestionConfig.id)
            .filter(ExtractionRun.workspace_id == workspace_id, IngestionConfig.status.in_(["approved", "draft"]))
            .distinct()
            .all()
        )
        # anche le bozze: un dataset con il mapping modificato torna in bozza finche' non viene reso disponibile
        # per l'analisi, e non deve sparire dalla lista (in cima, perche' c'e' qualcosa da fare)
        configs.sort(key=lambda c: (c.status != "draft", c.name))
        structures = []
        for config in configs:
            last_run = (
                db.query(ExtractionRun)
                .filter_by(workspace_id=workspace_id, ingestion_config_id=config.id)
                .order_by(ExtractionRun.started_at.desc())
                .first()
            )
            structures.append({"config": config, "last_run": last_run})
    finally:
        db.close()

    return templates.TemplateResponse(
        "structures.html",
        {"request": request, "user": user, "workspace_id": workspace_id, "process_name": ws.process_name, "structures": structures},
    )


def _with_derived_columns(rows: list[dict], data_profile: dict, lang: str) -> list[dict]:
    """Mapping salvato riaperto in revisione: le colonne calcolate che il profilo dei dati suggerisce oggi (log di
    modifiche, chiavi dentro valori composti) e che il mapping non ha ancora. Le righe nuove e quelle di attivita'
    che cambiano tornano «proposte»: nulla cambia senza conferma, e la versione precedente resta salvata."""
    fields = list(MappingProposal.__dataclass_fields__)
    props = [MappingProposal(**{k: r.get(k) for k in fields}) for r in rows]
    before = [asdict(p) for p in props]
    out = derived_columns.apply(props, data_profile, lang)
    result = []
    for i, r in enumerate(rows):
        now = asdict(out[i])
        if now != before[i]:
            r = {**r, **now, "status": "proposed"}
        result.append(r)
    next_id = max((r["row_id"] for r in rows), default=-1) + 1
    for j, p in enumerate(out[len(rows):]):
        d = asdict(p)
        d.update(row_id=next_id + j, status="proposed", original_ai_proposal={
            k: d[k] for k in ("ocel_element", "object_type", "event_type", "attribute_name", "qualifier",
                              "related_object_type", "activity_values", "confidence", "rationale")})
        result.append(d)
    return result


@router.get("/ingestion/structures/{config_id}/review")
async def review_existing_structure(request: Request, config_id: str, workspace_id: str):
    """Riapre la revisione del mapping di un dataset gia' generato (il Data Engineer vuole rivederlo):
    righe e decisioni salvate, tabelle rilette dai file caricati per questo processo. Rigenerando, si aggiorna
    lo stesso dataset (nuova versione)."""
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    db = SessionLocal()
    try:
        if config_id not in set(workspace_config_ids(db, workspace_id)):
            return RedirectResponse(url=f"/ingestion/structures?workspace_id={workspace_id}", status_code=303)
        config = db.get(IngestionConfig, config_id)
        saved = db.query(FieldMapping).filter_by(ingestion_config_id=config_id).all()
        fingerprint = dict(config.schema_fingerprint or {})
        business = config.business_objects
        name, version = config.name, config.current_version
        rows = []
        for i, r in enumerate(saved):
            rows.append({**_field_mapping_row_to_dict(r), "row_id": i, "status": r.status, "confidence": r.confidence,
                         "rationale": r.rationale or "", "based_on_template": r.based_on_template,
                         "original_ai_proposal": r.original_ai_proposal})
    finally:
        db.close()
    folder = UPLOAD_DIR / workspace_id
    files = [p for p in sorted(folder.iterdir()) if p.is_file() and p.stem in fingerprint] if folder.exists() else []
    missing = sorted(set(fingerprint) - {p.stem for p in files})
    if missing or not rows:
        return templates.TemplateResponse("message.html", {
            "request": request, "user": user, "workspace_id": workspace_id, "step": 3,
            "title": t(get_lang(request), "Revisione non disponibile"),
            "text": t(get_lang(request), "Mancano i file sorgente di questo dataset ({t}): ricaricali con «Ricarica i file e rifai il mapping» per rifare la revisione.",
                      t=", ".join(missing) or "—"),
            "back": f"/ingestion/structures?workspace_id={workspace_id}"}, status_code=409)

    sess = _load_session(user.id, workspace_id)
    sess["context"] = mapping_context(workspace_id)
    connector = FileConnector(files)
    tables_schema = connector.discover_schema()
    tables_data = {tb.name: connector.extract_full(tb.name) for tb in tables_schema}
    answers, _ = load_assessment(workspace_id)
    date_cols = {tb.name: [c.name for c in tb.columns if c.inferred_type == "date"] for tb in tables_schema}
    time_pairs = {tb.name: {c.name: c.time_column for c in tb.columns if c.time_column} for tb in tables_schema}
    planned = {tb.name: {c.name: c.planned_reason for c in tb.columns if c.planned_reason} for tb in tables_schema}
    sess["profile"] = await run_in_threadpool(
        profile_tables, tables_data, date_cols, (answers.get("period_from"), answers.get("period_to")),
        None, time_pairs, planned, [])
    sess["context"]["data_profile"] = compact_for_mapping(sess["profile"])
    sess["context"]["language"] = get_lang(request)
    rows = _with_derived_columns(rows, sess["context"]["data_profile"], get_lang(request))
    sess.update({
        "dataset_label": msg("{d}, versione {v}", d=name, v=version),
        "tables_schema": [asdict(tb) for tb in tables_schema], "tables_schema_objs": tables_schema,
        "tables_data": tables_data, "mapping_rows": rows, "mapping_status": "done", "mapping_error": None,
        "table_descriptions": {}, "result": None, "edit_config_id": config_id, "draft_config_id": None,
        "mapping_cost_usd": None, "mapping_known_tables": [], "mapping_budget_skipped": [], "process_order": None,
        "business_objects": business, "relevance": None,
    })
    if business:
        sess["context"]["business_objects"] = business_objects.for_context(business)
    return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}", status_code=303)


@router.get("/ingestion/structures/{config_id}/update-data", response_class=HTMLResponse)
def update_data_form(request: Request, config_id: str, workspace_id: str):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    db = SessionLocal()
    try:
        config = db.get(IngestionConfig, config_id)
    finally:
        db.close()
    return templates.TemplateResponse(
        "update_data.html",
        {"request": request, "user": user, "workspace_id": workspace_id, "config": config, "mode": "replace", "error": None},
    )


@router.post("/ingestion/structures/{config_id}/update-data")
async def update_data_submit(
    request: Request,
    config_id: str,
    workspace_id: str = Form(...),
    mode: str = Form("replace"),
    files: list[UploadFile] = File(default_factory=list),
):
    """mode="replace": il nuovo log contiene solo i dati appena caricati (sostituisce
    quelli pregressi). mode="append": i nuovi dati si aggiungono al log dell'ultimo
    run di questa struttura (vedi transformation.merge_ocel)."""
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    file_paths = _save_uploaded_files(files, UPLOAD_DIR / f"{config_id}-update")
    return _apply_update(request, user, workspace_id, config_id, file_paths, mode)


def _apply_update(request: Request, user, workspace_id: str, config_id: str, file_paths: list[Path], mode: str):
    """Riapplica il mapping confermato di una struttura a nuovi file (sostituendo
    o incrementando i dati). Usato da "Aggiorna dati" e dall'upload di una nuova
    struttura identica a una esistente."""
    if mode not in ("replace", "append"):
        mode = "replace"

    db = SessionLocal()
    try:
        config = db.get(IngestionConfig, config_id)
        confirmed_objects = (config.business_objects if config else None) or None
        last_run = (
            db.query(ExtractionRun)
            .filter_by(workspace_id=workspace_id, ingestion_config_id=config_id)
            .order_by(ExtractionRun.started_at.desc())
            .first()
        )
        previous_ocel_path = Path(last_run.ocel_file_path) if last_run else None
        mapping_rows = (
            db.query(FieldMapping)
            .filter_by(ingestion_config_id=config_id)
            .filter(FieldMapping.status.in_(["confirmed", "overridden"]))
            .all()
        )
        confirmed = [_field_mapping_row_to_dict(r) for r in mapping_rows]
    finally:
        db.close()

    if not file_paths:
        return templates.TemplateResponse(
            "update_data.html",
            {
                "request": request, "user": user, "workspace_id": workspace_id, "config": config, "mode": mode,
                "error": "Carica almeno un file CSV/TXT (o uno ZIP che li contenga) prima di continuare.",
            },
            status_code=400,
        )

    connector = FileConnector(file_paths)
    tables_schema = connector.discover_schema()
    new_fingerprint = _schema_fingerprint([
        {"name": t.name, "columns": [{"name": c.name} for c in t.columns]} for t in tables_schema
    ])

    problems = _check_schema_compatibility(new_fingerprint, confirmed)
    if problems:
        db = SessionLocal()
        try:
            config = db.get(IngestionConfig, config_id)
        finally:
            db.close()
        return templates.TemplateResponse(
            "update_data.html",
            {
                "request": request, "user": user, "workspace_id": workspace_id, "config": config, "mode": mode,
                "error": msg(
                    "I dati caricati non sono compatibili con il mapping di questo dataset, mancano: {p}. Usa "
                    "«Ricarica i file e rifai il mapping» per rimappare da zero, oppure carica dati nello stesso formato di prima.",
                    p=problems,
                ),
            },
            status_code=400,
        )

    tables_data = {t.name: connector.extract_full(t.name) for t in tables_schema}
    ocel, skip_log, stats = build_ocel(tables_data, confirmed)
    if mode == "append":
        if previous_ocel_path is None or not previous_ocel_path.exists():
            return templates.TemplateResponse(
                "update_data.html",
                {
                    "request": request, "user": user, "workspace_id": workspace_id, "config": config, "mode": mode,
                    "error": "Non trovo i dati precedenti di questo dataset a cui aggiungere i nuovi: usa «Sostituisci».",
                },
                status_code=400,
            )
        previous = json.loads(previous_ocel_path.read_text(encoding="utf-8"))
        ocel, delta = merge_ocel(previous, ocel)
        stats = {
            **stats, **delta,
            "object_count": len(ocel["objects"]),
            "event_count": len(ocel["events"]),
        }
    stats["update_mode"] = mode
    dq_results = run_data_quality_checks(
        ocel, skip_log, stats, _main_object(workspace_id), _planned_events(confirmed, tables_schema),
        business_objects=confirmed_objects,
    )

    ocel_path = OUTPUT_DIR / f"{config_id}-{uuid.uuid4().hex[:8]}.ocel.json"
    ocel_path.write_text(json.dumps(ocel, indent=2, ensure_ascii=False), encoding="utf-8")

    db = SessionLocal()
    try:
        run = ExtractionRun(
            workspace_id=workspace_id, ingestion_config_id=config_id,
            run_type="incremental" if mode == "append" else "snapshot", status="completed",
            object_count=stats["object_count"], event_count=stats["event_count"],
            ocel_file_path=str(ocel_path),
        )
        db.add(run)
        db.flush()
        for dq in dq_results:
            db.add(DataQualityCheckResult(
                extraction_run_id=run.id, check_name=dq["check_name"], severity=dq["severity"],
                passed=dq["passed"], details=to_text(dq["details"]), affected_count=dq["affected_count"],
            ))
        db.commit()
    finally:
        db.close()

    sess = _load_session(user.id, workspace_id)
    sess["result"] = {
        "ocel_path": str(ocel_path),
        "stats": stats,
        "dq_results": dq_results,
        "ingestion_config_id": config_id,
        "rejected_count": 0,
        "overridden_count": 0,
    }
    return RedirectResponse(f"/ingestion/result?workspace_id={workspace_id}", status_code=303)


@router.post("/ingestion/structures/{config_id}/delete")
def delete_structure(request: Request, config_id: str, workspace_id: str = Form(...)):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied

    db = SessionLocal()
    try:
        if db.get(IngestionConfig, config_id) is None:
            return RedirectResponse(f"/ingestion/structures?workspace_id={workspace_id}", status_code=303)
        files = delete_structures(db, [config_id])
        db.commit()
    finally:
        db.close()
    remove_files(files)

    return RedirectResponse(f"/ingestion/structures?workspace_id={workspace_id}", status_code=303)


@router.get("/ingestion/download/{workspace_id}")
def download_ocel(request: Request, workspace_id: str):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    path = sess["result"]["ocel_path"]
    return FileResponse(path, media_type="application/json", filename="event_log.ocel.json")


@router.get("/ingestion/runs/{run_id}/download")
def download_run(request: Request, run_id: str, workspace_id: str):
    # Scaricare un log OCEL gia' generato serve sia a chi lo produce (Data
    # Engineer, Modulo 1) sia a chi lo consuma (Data Analyst, Modulo 2):
    # a differenza delle altre rotte di ingestion.py, qui basta uno dei due ruoli.
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not (
        has_process_access(user, workspace_id, required_role="data_engineer")
        or has_process_access(user, workspace_id, required_role="data_analyst")
    ):
        return HTMLResponse(t(get_lang(request), "Accesso negato: non sei assegnato come Data Engineer o Data Analyst a questo processo."),
            status_code=403,
        )
    db = SessionLocal()
    try:
        run = db.get(ExtractionRun, run_id)
    finally:
        db.close()
    if run is None or run.workspace_id != workspace_id:
        return HTMLResponse(t(get_lang(request), "Run non trovato."), status_code=404)
    return FileResponse(run.ocel_file_path, media_type="application/json", filename="event_log.ocel.json")
