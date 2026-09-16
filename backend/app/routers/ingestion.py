from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import current_user, has_process_access
from app.config import AI_MAPPER, AUTO_ACCEPT_CONFIDENCE_THRESHOLD, DATA_DIR
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
from app.services.transformation import build_ocel, compile_defs
from app.services.validation import run_data_quality_checks

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

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
    """Ritorna (user, None) se autorizzato al Modulo 1 di questo processo,
    altrimenti (None, redirect_o_403)."""
    user = current_user(request)
    if user is None:
        return None, RedirectResponse("/login", status_code=303)
    if not has_process_access(user, workspace_id):
        return None, HTMLResponse(
            "Accesso negato: non sei assegnato come Data Engineer a questo processo.",
            status_code=403,
        )
    return user, None


def _load_session(workspace_id: str) -> dict:
    """Stato in-memory per il workspace, inizializzato al volo dal DB se e' la
    prima visita di questa run del server (vedi state.py)."""
    sess = state.ensure(workspace_id)
    if "context" not in sess:
        db = SessionLocal()
        try:
            ws = db.get(ProcessWorkspace, workspace_id)
        finally:
            db.close()
        sess["context"] = {
            "process_name": ws.process_name,
            "process_type": ws.process_type,
            "business_unit": ws.business_unit or "",
            "period_from": ws.period_from or "",
            "period_to": ws.period_to or "",
        }
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
    if current_user(request) is None:
        return RedirectResponse(url="/login")
    return RedirectResponse(url="/ingestion/dashboard")


@router.get("/ingestion/dashboard", response_class=HTMLResponse)
def ingestion_dashboard(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    db = SessionLocal()
    try:
        if user.is_admin:
            workspaces = db.query(ProcessWorkspace).order_by(ProcessWorkspace.created_at.desc()).all()
        else:
            assigned_ids = [
                a.workspace_id
                for a in db.query(ProcessAssignment).filter_by(user_id=user.id, role="data_engineer").all()
            ]
            workspaces = (
                db.query(ProcessWorkspace)
                .filter(ProcessWorkspace.id.in_(assigned_ids))
                .order_by(ProcessWorkspace.created_at.desc())
                .all()
                if assigned_ids
                else []
            )
    finally:
        db.close()

    return templates.TemplateResponse(
        "ingestion_dashboard.html", {"request": request, "user": user, "workspaces": workspaces}
    )


@router.get("/ingestion/new", response_class=HTMLResponse)
def new_context_form(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not user.is_admin:
        return HTMLResponse("Accesso negato: solo l'amministratore può creare un nuovo processo.", status_code=403)
    return templates.TemplateResponse("context.html", {"request": request, "user": user, "step": 1})


@router.post("/ingestion/new")
def create_workspace(
    request: Request,
    process_name: str = Form(...),
    process_type: str = Form(...),
    business_unit: str = Form(""),
    period_from: str = Form(""),
    period_to: str = Form(""),
):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not user.is_admin:
        return HTMLResponse("Accesso negato: solo l'amministratore può creare un nuovo processo.", status_code=403)

    db = SessionLocal()
    try:
        ws = ProcessWorkspace(
            process_name=process_name,
            process_type=process_type,
            business_unit=business_unit or None,
            period_from=period_from or None,
            period_to=period_to or None,
            created_by=user.name,
        )
        db.add(ws)
        db.commit()
    finally:
        db.close()

    # Il wizard di Ingestion (Fasi B-G) lo porta avanti il Data Engineer assegnato,
    # non l'admin: si torna alla dashboard admin per fare l'assegnazione.
    return RedirectResponse(url="/admin", status_code=303)


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
    sess = _load_session(workspace_id)
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
        }
    )


@router.post("/ingestion/upload")
async def handle_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    workspace_id: str = Form(...),
    files: list[UploadFile] = File(default_factory=list),
):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(workspace_id)

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

    return RedirectResponse(url=f"/ingestion/describe-tables?workspace_id={workspace_id}", status_code=303)


@router.get("/ingestion/describe-tables", response_class=HTMLResponse)
def describe_tables_page(request: Request, workspace_id: str):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(workspace_id)
    tables_schema = sess.get("tables_schema_objs")
    if not tables_schema:
        return RedirectResponse(url=f"/ingestion/upload?workspace_id={workspace_id}", status_code=303)
    descriptions = sess.get("table_descriptions", {})
    return templates.TemplateResponse(
        "describe_tables.html", {
            "request": request, "user": user, "workspace_id": workspace_id, "context": sess["context"],
            "tables": tables_schema, "descriptions": descriptions, "step": 2,
        }
    )


@router.post("/ingestion/describe-tables")
async def submit_table_descriptions(request: Request, background_tasks: BackgroundTasks, workspace_id: str = Form(...)):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(workspace_id)
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

    sess["mapping_rows"] = None
    sess["mapping_status"] = "pending"
    sess["mapping_error"] = None

    background_tasks.add_task(
        _run_ai_mapping, sess, tables_schema, sess["tables_data"], sess["dataset_label"], descriptions
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
    sess = _load_session(workspace_id)
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
    sess = _load_session(workspace_id)
    rows = sess.get("mapping_rows")
    if rows is None:
        # Non ancora pronto (o mai partito, es. link diretto): manda alla pagina
        # di attesa invece di un errore, e' quella che sa cosa fare in ogni stato.
        return RedirectResponse(url=f"/ingestion/mapping-status?workspace_id={workspace_id}", status_code=303)
    for r in rows:
        r["target_label"] = _target_label(r)

    by_table: dict[str, list[dict]] = {}
    for r in rows:
        by_table.setdefault(r["source_table"], []).append(r)

    pending_count = sum(1 for r in rows if r["status"] == "proposed")

    blocked_message = None
    if error == "pending" and pending_count > 0:
        blocked_message = (
            f"Non ho generato il log: ci sono ancora {pending_count} proposte senza una decisione "
            "esplicita (righe evidenziate in giallo qui sotto). Accettale, rifiutale o modificale "
            "prima di confermare — oppure usa \"Accetta tutte ≥ soglia\" per sbrigare in blocco "
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
            "pending_count": pending_count,
            "threshold": AUTO_ACCEPT_CONFIDENCE_THRESHOLD,
            "ocel_elements": VALID_OCEL_ELEMENTS,
            "blocked_message": blocked_message,
            "step": 3,
        },
    )


@router.post("/ingestion/review")
async def submit_review(request: Request, workspace_id: str = Form(...), action: str = Form(...)):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(workspace_id)
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

    if action == "finalize":
        still_pending = [r for r in rows if r["status"] == "proposed"]
        if still_pending:
            return RedirectResponse(
                url=f"/ingestion/review?workspace_id={workspace_id}&error=pending", status_code=303
            )
        _finalize(workspace_id, sess, user)
        return RedirectResponse(url=f"/ingestion/result?workspace_id={workspace_id}", status_code=303)

    return RedirectResponse(url=f"/ingestion/review?workspace_id={workspace_id}", status_code=303)


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
                name=f"{ctx['process_type']} - {source_system.name}",
                source_system_id=source_system.id,
                process_type=ctx["process_type"],
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
    sess = _load_session(workspace_id)
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
            "step": 4,
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
        {"request": request, "user": user, "workspace_id": workspace_id, "config": config, "error": None},
    )


@router.post("/ingestion/structures/{config_id}/update-data")
async def update_data_submit(
    request: Request,
    config_id: str,
    workspace_id: str = Form(...),
    files: list[UploadFile] = File(default_factory=list),
):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied

    db = SessionLocal()
    try:
        config = db.get(IngestionConfig, config_id)
        mapping_rows = (
            db.query(FieldMapping)
            .filter_by(ingestion_config_id=config_id)
            .filter(FieldMapping.status.in_(["confirmed", "overridden"]))
            .all()
        )
        confirmed = [_field_mapping_row_to_dict(r) for r in mapping_rows]
    finally:
        db.close()

    file_paths = _save_uploaded_files(files, UPLOAD_DIR / f"{config_id}-update")

    if not file_paths:
        return templates.TemplateResponse(
            "update_data.html",
            {
                "request": request, "user": user, "workspace_id": workspace_id, "config": config,
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
                "request": request, "user": user, "workspace_id": workspace_id, "config": config,
                "error": (
                    "I dati caricati non sono compatibili con questa struttura, "
                    f"mancano: {', '.join(problems)}. Usa \"Modifica struttura\" per rimappare "
                    "da zero, oppure carica dati nello stesso formato di prima."
                ),
            },
            status_code=400,
        )

    tables_data = {t.name: connector.extract_full(t.name) for t in tables_schema}
    ocel, skip_log, stats = build_ocel(tables_data, confirmed)
    dq_results = run_data_quality_checks(ocel, skip_log)

    ocel_path = OUTPUT_DIR / f"{config_id}-{uuid.uuid4().hex[:8]}.ocel.json"
    ocel_path.write_text(json.dumps(ocel, indent=2, ensure_ascii=False), encoding="utf-8")

    db = SessionLocal()
    try:
        run = ExtractionRun(
            workspace_id=workspace_id, ingestion_config_id=config_id,
            run_type="incremental", status="completed",
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

    sess = _load_session(workspace_id)
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
        config = db.get(IngestionConfig, config_id)
        if config is None:
            return RedirectResponse(f"/ingestion/structures?workspace_id={workspace_id}", status_code=303)

        run_ids = [r.id for r in db.query(ExtractionRun).filter_by(ingestion_config_id=config_id).all()]
        ocel_paths = [r.ocel_file_path for r in db.query(ExtractionRun).filter_by(ingestion_config_id=config_id).all()]

        db.query(DataQualityCheckResult).filter(DataQualityCheckResult.extraction_run_id.in_(run_ids)).delete(
            synchronize_session=False
        )
        db.query(ExtractionRun).filter_by(ingestion_config_id=config_id).delete(synchronize_session=False)
        db.query(ProcessIngestionLink).filter_by(ingestion_config_id=config_id).delete(synchronize_session=False)
        db.query(FieldMapping).filter_by(ingestion_config_id=config_id).delete(synchronize_session=False)
        db.query(ObjectTypeDef).filter_by(ingestion_config_id=config_id).delete(synchronize_session=False)
        db.query(EventTypeDef).filter_by(ingestion_config_id=config_id).delete(synchronize_session=False)
        db.query(IngestionConfigVersion).filter_by(ingestion_config_id=config_id).delete(synchronize_session=False)
        db.delete(config)
        db.commit()
    finally:
        db.close()

    for p in ocel_paths:
        Path(p).unlink(missing_ok=True)

    return RedirectResponse(f"/ingestion/structures?workspace_id={workspace_id}", status_code=303)


@router.get("/ingestion/download/{workspace_id}")
def download_ocel(request: Request, workspace_id: str):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    sess = _load_session(workspace_id)
    path = sess["result"]["ocel_path"]
    return FileResponse(path, media_type="application/json", filename="event_log.ocel.json")


@router.get("/ingestion/runs/{run_id}/download")
def download_run(request: Request, run_id: str, workspace_id: str):
    user, denied = _require_process_access(request, workspace_id)
    if denied:
        return denied
    db = SessionLocal()
    try:
        run = db.get(ExtractionRun, run_id)
    finally:
        db.close()
    if run is None or run.workspace_id != workspace_id:
        return HTMLResponse("Run non trovato.", status_code=404)
    return FileResponse(run.ocel_file_path, media_type="application/json", filename="event_log.ocel.json")
