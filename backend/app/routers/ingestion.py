from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from dataclasses import asdict, replace
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import current_user, has_process_access
from app.config import AI_MAPPER, AUTO_ACCEPT_CONFIDENCE_THRESHOLD, DATA_DIR, STATIC_VERSION
from app.connectors.file_connector import FileConnector
from app.db import SessionLocal
from app.models import (
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
from app.services.ai_mapping import AIMapper, ClaudeAIMapper, HeuristicAIMapper
from app.routers.assessment import assessment_status, load_assessment, mapping_context
from app.services.profiling import compact_for_mapping, profile_tables
from app.services.preview import build_preview, summary_for_ai
from app.services.analysis_capabilities import assess_capabilities, fallback_capabilities
from app.services.relevance import check_relevance
from app.services.structures import delete_structures, remove_files, workspace_config_ids
from app.services.transformation import build_ocel, compile_defs, merge_ocel
from app.services.validation import run_data_quality_checks

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals["static_version"] = STATIC_VERSION

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
        return None, HTMLResponse(
            "Accesso negato: non sei assegnato come Data Engineer a questo processo.",
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
        "related_object_type": r.related_object_type,
    }


def _check_schema_compatibility(new_fingerprint: dict[str, list[str]], confirmed: list[dict]) -> list[str]:
    """Ritorna la lista di tabelle/colonne che il mapping confermato richiede
    ma che non sono presenti nel nuovo caricamento; lista vuota = compatibile.
    Colonne extra nel nuovo caricamento non sono un problema: contano solo
    quelle effettivamente usate dal mapping."""
    needed = {
        (r["source_table"], r["source_column"])
        for r in confirmed
        if r.get("source_table") and r.get("source_column")
    }
    missing_tables = {table for table, _ in needed if table not in new_fingerprint}
    problems = [f"tabella mancante: \"{table}\"" for table in sorted(missing_tables)]
    for table, column in sorted(needed):
        if table not in missing_tables and column not in new_fingerprint[table]:
            problems.append(f"colonna mancante: \"{table}.{column}\"")
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
    finally:
        db.close()

    return templates.TemplateResponse(
        "ingestion_dashboard.html",
        {
            "request": request, "user": user, "workspaces": workspaces, "roles_by_workspace": roles_by_workspace,
            "assessments": {
                ws.id: assessment_status(ws.id) for ws in workspaces
                if "data_engineer" in roles_by_workspace.get(ws.id, set())
            },
        },
    )


EDITABLE_FIELDS = ["ocel_element", "object_type", "event_type", "attribute_name", "qualifier", "related_object_type"]
VALID_OCEL_ELEMENTS = [
    "object_type.key", "object_type.attribute",
    "event_type.timestamp", "event_type.attribute", "e2o_relationship",
]


def _target_label(r: dict) -> str:
    v = lambda k: r.get(k) or "—"  # noqa: E731
    el = r["ocel_element"]
    if el == "object_type.key":
        return f'Chiave oggetto → {v("object_type")}'
    if el == "object_type.attribute":
        return f'Attributo oggetto → {v("object_type")}.{v("attribute_name")}'
    if el == "event_type.timestamp":
        return f'Timestamp evento → "{v("event_type")}"'
    if el == "event_type.attribute":
        return f'Attributo evento → "{v("event_type")}".{v("attribute_name")}'
    if el == "e2o_relationship":
        return f'Relazione evento→oggetto → "{v("event_type")}" —[{v("qualifier")}]→ {v("related_object_type")}'
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
    dataset_label = f"{len(file_paths)} file caricati"

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
    sess["profile"] = await run_in_threadpool(
        profile_tables, tables_data, date_cols, (answers.get("period_from"), answers.get("period_to"))
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
        }
    )


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
    # le evidenze misurate (chiavi, date, collegamenti) arrivano all'AI Mapping con il contesto
    sess["context"]["data_profile"] = compact_for_mapping(sess["profile"])
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

    sess["mapping_rows"] = None
    sess["mapping_status"] = "pending"
    sess["mapping_error"] = None

    background_tasks.add_task(
        _run_ai_mapping, sess, _without_empty_columns(tables_schema), sess["tables_data"], sess["dataset_label"], descriptions
    )

    return RedirectResponse(url=f"/ingestion/mapping-status?workspace_id={workspace_id}", status_code=303)


def _run_ai_mapping(
    sess: dict, tables_schema: list, tables_data: dict, dataset_label: str, table_descriptions: dict[str, str]
) -> None:
    """Gira dopo che la risposta HTTP e' gia' stata inviata (FastAPI BackgroundTasks):
    scrive l'esito in `sess`, che /ingestion/mapping-status legge via polling."""
    try:
        mapper, mapper_label = _get_ai_mapper()
        try:
            proposals = mapper.propose_mapping(tables_schema, sess["context"], table_descriptions)
        except Exception as exc:
            if mapper_label == "euristica mock":
                raise
            # La chiamata Claude puo' fallire per motivi esterni (rete, chiave non
            # valida, rate limit): meglio un fallback trasparente sull'euristica
            # mock, con l'errore reale visibile nei log e in etichetta, che un 500.
            print(f"ClaudeAIMapper ha fallito ({exc!r}): fallback sull'euristica mock per questo upload.")
            mapper_label = "euristica mock (fallback: chiamata Claude fallita)"
            proposals = HeuristicAIMapper().propose_mapping(tables_schema, sess["context"], table_descriptions)

        rows = []
        for i, p in enumerate(proposals):
            d = asdict(p)
            d["row_id"] = i
            # Tutto parte come "proposed": e' il pulsante "Accetta tutte >= soglia" a promuovere
            # le righe ad alta confidence a "confirmed" in un click, esplicitamente. Pre-confermarle
            # gia' qui renderebbe quel pulsante un no-op silenzioso (bug reale trovato in test).
            d["status"] = "proposed"
            # snapshot immutabile di cio' che l'AI ha proposto in origine: sopravvive a
            # eventuali correzioni manuali successive, per audit trail (FieldMapping.original_ai_proposal)
            d["original_ai_proposal"] = {
                "ocel_element": p.ocel_element, "object_type": p.object_type, "event_type": p.event_type,
                "attribute_name": p.attribute_name, "qualifier": p.qualifier,
                "related_object_type": p.related_object_type, "confidence": p.confidence, "rationale": p.rationale,
            }
            rows.append(d)

        sess["dataset_label"] = f"{dataset_label} · AI Mapping Service: {mapper_label}"
        sess["mapping_rows"] = rows
        sess["mapping_status"] = "done"
    except Exception as exc:
        print(f"Generazione mapping fallita del tutto ({exc!r}).")
        sess["mapping_status"] = "error"
        sess["mapping_error"] = str(exc)


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
        }
    )


@router.get("/ingestion/review", response_class=HTMLResponse)
def review_page(request: Request, workspace_id: str, error: str | None = None):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    rows = sess.get("mapping_rows")
    if rows is None:
        # Non ancora pronto (o mai partito, es. link diretto): manda alla pagina
        # di attesa invece di un errore, e' quella che sa cosa fare in ogni stato.
        return RedirectResponse(url=f"/ingestion/mapping-status?workspace_id={workspace_id}", status_code=303)
    for r in rows:
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
        blocked_message = (
            f"Non ho generato il dataset: ci sono ancora {pending_count} proposte senza una decisione "
            "esplicita (righe evidenziate in giallo qui sotto). Accettale, rifiutale o modificale "
            "prima di confermare — oppure usa \"Accetta le proposte ≥ soglia\" per sbrigare in blocco "
            "quelle ad alta confidence."
        )

    return templates.TemplateResponse(
        "mapping_review.html",
        {
            "request": request,
            "user": user,
            "workspace_id": workspace_id,
            "context": sess["context"],
            "dataset_label": sess["dataset_label"],
            "by_table": by_table,
            "tables_to_check": tables_to_check,
            "groups_by_table": groups_by_table,
            "model": _model_summary(rows, order),
            "pending_count": pending_count,
            "threshold": AUTO_ACCEPT_CONFIDENCE_THRESHOLD,
            "ocel_elements": VALID_OCEL_ELEMENTS,
            "blocked_message": blocked_message,
            "step": 3,
        },
    )


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
    tenuto in cache finche' il mapping non cambia."""
    used = _preview_rows(rows)
    signature = _rows_signature(used, None)
    cached = sess.get("process_order")
    if cached and cached["signature"] == signature:
        return cached["order"]
    events_order: dict[str, float] = {}
    objects_order: dict[str, float] = {}
    try:
        ocel, _, _ = build_ocel(sess["tables_data"], used)
        times: dict[str, list[str]] = {}
        for e in ocel["events"]:
            times.setdefault(e["type"], []).append(e["time"])
        ranked = sorted(times, key=lambda t: sorted(times[t])[len(times[t]) // 2])
        events_order = {t: i for i, t in enumerate(ranked)}
        obj_type = {o["id"]: o["type"] for o in ocel["objects"]}
        linked_events: dict[str, set] = {}
        for e in ocel["events"]:
            for r in e["relationships"]:
                t = obj_type.get(r["objectId"])
                if t is not None:
                    objects_order[t] = min(objects_order.get(t, 1e9), events_order[e["type"]])
                    linked_events.setdefault(t, set()).add(e["type"])
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
            continue
        if r["status"] == "proposed":
            d["pending"] += 1
            if r["confidence"] < AUTO_ACCEPT_CONFIDENCE_THRESHOLD:
                d["low"] += 1
        el = r["ocel_element"]
        if el == "object_type.key":
            d["keys"].append(r["source_column"])
        elif el == "event_type.timestamp":
            d["timestamps"].append(f"{r['source_table']}.{r['source_column']}")
        elif el == "e2o_relationship" and r.get("related_object_type"):
            if r["related_object_type"] not in d["links"]:
                d["links"].append(r["related_object_type"])
        else:
            d["attributes"] += 1
    order = order or {"events": {}, "objects": {}}

    def key(g, positions):  # ordine di processo; gruppi rifiutati in fondo
        return (g["rejected"] == g["total"], positions.get(g["name"], 1e9), g["name"])
    objects = sorted((g for g in groups.values() if g["kind"] == "obj"), key=lambda g: key(g, order["objects"]))
    events = sorted((g for g in groups.values() if g["kind"] == "evt"), key=lambda g: key(g, order["events"]))
    return {"objects": objects, "events": events}


@router.post("/ingestion/review")
async def submit_review(request: Request, workspace_id: str = Form(...), action: str = Form(...)):
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
        return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}", status_code=303)

    # decisione per gruppo del modello: "group_accept|obj|PurchaseOrder"
    if action.startswith(("group_accept|", "group_reject|")):
        verb, kind, name = action.split("|", 2)
        new_status = "confirmed" if verb == "group_accept" else "rejected"
        for r in rows:
            if _row_group(r) == (kind, name) and (r["status"] == "proposed" or new_status == "rejected"):
                r["status"] = new_status
        return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}#modello", status_code=303)

    if action == "preview":
        return RedirectResponse(url=f"/ingestion/preview?workspace_id={workspace_id}", status_code=303)

    if action == "finalize":
        still_pending = [r for r in rows if r["status"] == "proposed"]
        if still_pending:
            return RedirectResponse(
                url=f"/ingestion/review?workspace_id={workspace_id}&error=pending", status_code=303
            )
        _finalize(workspace_id, sess, user)
        return RedirectResponse(url=f"/ingestion/result?workspace_id={workspace_id}", status_code=303)

    return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}", status_code=303)


def _preview_rows(rows: list[dict]) -> list[dict]:
    """Righe usate per l'anteprima: tutto tranne i rifiuti (anche le proposte non
    ancora decise, cosi' l'anteprima e' utile anche a meta' revisione)."""
    return [r for r in rows if r["status"] != "rejected"]


def _rows_signature(rows: list[dict], case_type: str | None) -> str:
    import hashlib
    key = json.dumps(
        [[r["source_table"], r["source_column"], r["ocel_element"], r.get("object_type"), r.get("event_type"),
          r.get("attribute_name"), r.get("related_object_type")] for r in rows] + [case_type],
        ensure_ascii=False,
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


@router.get("/ingestion/preview", response_class=HTMLResponse)
def preview_page(request: Request, workspace_id: str, case: str | None = None):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    rows = sess.get("mapping_rows")
    if rows is None:
        return RedirectResponse(url=f"/ingestion/mapping-status?workspace_id={workspace_id}", status_code=303)
    used = _preview_rows(rows)
    preview = build_preview(sess["tables_data"], used, case or sess.get("preview_case"))
    sess["preview_case"] = preview["case_type"]
    sess["preview_summary"] = summary_for_ai(preview, used)
    sess["preview_signature"] = _rows_signature(used, preview["case_type"])
    return templates.TemplateResponse(
        "preview.html", {
            "request": request, "user": user, "workspace_id": workspace_id, "context": sess["context"],
            "preview": preview, "pending_count": sum(1 for r in rows if r["status"] == "proposed"), "step": 4,
        }
    )


@router.get("/ingestion/preview/capabilities", response_class=HTMLResponse)
async def preview_capabilities(request: Request, workspace_id: str, refresh: int = 0):
    """Frammento HTML "Cosa potrai analizzare", caricato dalla pagina di anteprima.
    Ricalcolato (chiamata AI) solo se il mapping o l'oggetto principale sono cambiati."""
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    summary = sess.get("preview_summary")
    if summary is None:
        return HTMLResponse("", status_code=204)
    cached = sess.get("capabilities")
    if not refresh and cached and cached["signature"] == sess["preview_signature"]:
        caps, source = cached["data"], cached["source"]
    else:
        source = "ai"
        caps = None
        if AI_MAPPER == "claude":
            try:
                caps = (await run_in_threadpool(assess_capabilities, sess["context"], summary)).model_dump()
            except Exception as exc:
                print(f"Valutazione analisi possibili non riuscita ({exc!r}): uso la versione di base.")
        if caps is None:
            caps, source = fallback_capabilities(summary).model_dump(), "base"
        sess["capabilities"] = {"signature": sess["preview_signature"], "data": caps, "source": source}
    return templates.TemplateResponse(
        "_capabilities.html", {"request": request, "caps": caps, "source": source, "workspace_id": workspace_id}
    )


def _finalize(workspace_id: str, sess: dict, user: User) -> None:
    rows = sess["mapping_rows"]
    confirmed = [r for r in rows if r["status"] in ("confirmed", "overridden")]

    ocel, skip_log, stats = build_ocel(sess["tables_data"], confirmed)
    dq_results = run_data_quality_checks(ocel, skip_log)
    object_defs, event_defs = compile_defs(confirmed)
    schema_fp = _schema_fingerprint(sess["tables_schema"])
    edit_config_id = sess.get("edit_config_id")

    ocel_path = OUTPUT_DIR / f"{workspace_id}-{uuid.uuid4().hex[:8]}.ocel.json"
    ocel_path.write_text(json.dumps(ocel, indent=2, ensure_ascii=False), encoding="utf-8")

    db = SessionLocal()
    try:
        ctx = sess["context"]

        if edit_config_id:
            config = db.get(IngestionConfig, edit_config_id)
            config.current_version += 1
            config.schema_fingerprint = schema_fp
            # richiede una nuova promozione esplicita: non torna attiva per l'analisi da sola
            config.status = "draft"
            db.add(IngestionConfigVersion(
                ingestion_config_id=config.id, version=config.current_version,
                changelog="Struttura rigenerata dal Data Engineer (nuovo mapping su nuovi dati).",
                approved_by=user.name,
            ))
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

        caps = sess.get("capabilities")
        if caps and caps["signature"] == _rows_signature(_preview_rows(rows), sess.get("preview_case")):
            config.analysis_capabilities = {**caps["data"], "source": caps["source"]}

        for od in object_defs.values():
            db.add(ObjectTypeDef(
                ingestion_config_id=config.id, name=od.name,
                source_table=od.source_table, key_columns=",".join(od.key_columns),
            ))
        for ed in event_defs.values():
            db.add(EventTypeDef(
                ingestion_config_id=config.id, name=ed.name,
                source_table=ed.source_table, timestamp_column=ed.timestamp_column,
            ))

        for r in rows:
            overridden = r["status"] == "overridden"
            db.add(FieldMapping(
                ingestion_config_id=config.id,
                source_table=r["source_table"], source_column=r["source_column"],
                ocel_element=r["ocel_element"], object_type=r["object_type"], event_type=r["event_type"],
                attribute_name=r["attribute_name"], qualifier=r["qualifier"],
                related_object_type=r["related_object_type"],
                proposal_source="user" if overridden else "ai",
                confidence=r["confidence"], rationale=r["rationale"],
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
                passed=dq["passed"], details=dq["details"], affected_count=dq["affected_count"],
            ))

        db.commit()
        ingestion_config_id = config.id
    finally:
        db.close()

    sess["result"] = {
        "ocel_path": str(ocel_path),
        "stats": stats,
        "dq_results": dq_results,
        "ingestion_config_id": ingestion_config_id,
        "rejected_count": sum(1 for r in rows if r["status"] == "rejected"),
        "overridden_count": sum(1 for r in rows if r["status"] == "overridden"),
    }


@router.get("/ingestion/result", response_class=HTMLResponse)
def result_page(request: Request, workspace_id: str):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(user.id, workspace_id)
    result = sess["result"]

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
            .filter(ExtractionRun.workspace_id == workspace_id, IngestionConfig.status == "approved")
            .distinct()
            .all()
        )
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
                "error": (
                    "I dati caricati non sono compatibili con il mapping di questo dataset, "
                    f"mancano: {', '.join(problems)}. Usa \"Modifica mapping\" per rimappare "
                    "da zero, oppure carica dati nello stesso formato di prima."
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
                    "error": "Non trovo i dati precedenti di questo dataset a cui aggiungere i nuovi: usa \"Sostituisci\".",
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
    dq_results = run_data_quality_checks(ocel, skip_log)

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
                passed=dq["passed"], details=dq["details"], affected_count=dq["affected_count"],
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
        return HTMLResponse(
            "Accesso negato: non sei assegnato come Data Engineer o Data Analyst a questo processo.",
            status_code=403,
        )
    db = SessionLocal()
    try:
        run = db.get(ExtractionRun, run_id)
    finally:
        db.close()
    if run is None or run.workspace_id != workspace_id:
        return HTMLResponse("Run non trovato.", status_code=404)
    return FileResponse(run.ocel_file_path, media_type="application/json", filename="event_log.ocel.json")
