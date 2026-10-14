from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import state
from app.auth import current_user, hash_password
from app.i18n import get_lang, msg, setup_templates, t
from app.config import DATA_DIR, STATIC_VERSION
from app.db import SessionLocal
from app.models import (
    DataQualityCheckResult,
    ObjectiveCoverage,
    ExtractionRun,
    IngestionConfig,
    ProcessAssessment,
    ProcessAssignment,
    ProcessDocument,
    ProcessIngestionLink,
    ProcessWorkspace,
    User,
)
from app.services import llm
from app.services.structures import delete_structures, remove_files, workspace_config_ids

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals["static_version"] = STATIC_VERSION
setup_templates(templates)


def _require_admin(request: Request):
    """Ritorna (user, None) se autorizzato, altrimenti (None, redirect_o_403)."""
    user = current_user(request)
    if user is None:
        return None, RedirectResponse("/login", status_code=303)
    if not user.is_admin:
        return None, HTMLResponse(
            t(get_lang(request), "Accesso negato: questa pagina è riservata agli amministratori."), status_code=403
        )
    return user, None


ASSIGNABLE_ROLES = ["data_engineer", "data_analyst"]
ROLE_LABELS = {"data_engineer": "Data Engineer", "data_analyst": "Data Analyst"}


@router.get("", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    user, denied = _require_admin(request)
    if denied:
        return denied

    db = SessionLocal()
    try:
        workspaces = db.query(ProcessWorkspace).order_by(ProcessWorkspace.created_at.desc()).all()
        users_by_id = {u.id: u for u in db.query(User).all()}
        # una entry per ogni (workspace, ruolo) assegnato: uno stesso processo puo'
        # avere sia un Data Engineer sia un Data Analyst assegnati, righe distinte
        assigned_user_by_workspace_role = {
            (a.workspace_id, a.role): users_by_id.get(a.user_id)
            for a in db.query(ProcessAssignment).filter(ProcessAssignment.role.in_(ASSIGNABLE_ROLES)).all()
        }
        configs_by_id = {c.id: c for c in db.query(IngestionConfig).all()}
        rows = [
            {
                "workspace": ws,
                "structures": [
                    configs_by_id[cid] for cid in workspace_config_ids(db, ws.id) if cid in configs_by_id
                ],
                "assignments": {
                    role: assigned_user_by_workspace_role.get((ws.id, role))
                    for role in ASSIGNABLE_ROLES
                },
            }
            for ws in workspaces
        ]
        assignable_users = [u for u in users_by_id.values() if not u.is_admin]
        all_users = sorted(users_by_id.values(), key=lambda u: u.name)
    finally:
        db.close()

    return templates.TemplateResponse(
        "admin_dashboard.html",
        {
            "request": request, "user": user, "rows": rows, "assignable_users": assignable_users,
            "all_users": all_users, "assignable_roles": ASSIGNABLE_ROLES, "role_labels": ROLE_LABELS,
        },
    )


def _assignable_users(db) -> list[User]:
    return sorted(db.query(User).filter_by(is_admin=False).all(), key=lambda u: u.name)


def _new_process_page(request: Request, user: User, error: str | None = None, status_code: int = 200):
    db = SessionLocal()
    try:
        assignable_users = _assignable_users(db)
    finally:
        db.close()
    return templates.TemplateResponse(
        "admin_new_process.html",
        {
            "request": request, "user": user, "error": error, "assignable_users": assignable_users,
            "assignable_roles": ASSIGNABLE_ROLES, "role_labels": ROLE_LABELS,
        },
        status_code=status_code,
    )


@router.get("/processes/new", response_class=HTMLResponse)
def new_process_form(request: Request):
    user, denied = _require_admin(request)
    if denied:
        return denied
    return _new_process_page(request, user)


@router.post("/processes/new")
def create_process(
    request: Request,
    process_name: str = Form(...),
    data_engineer: str = Form(""),
    data_analyst: str = Form(""),
):
    """Crea il processo (solo il nome) e, se scelti, assegna subito Data
    Engineer e Data Analyst; le assegnazioni restano modificabili dalla
    dashboard di Amministrazione."""
    user, denied = _require_admin(request)
    if denied:
        return denied
    process_name = process_name.strip()
    if not process_name:
        return _new_process_page(request, user, "Il nome del processo è obbligatorio.", 400)

    chosen = {"data_engineer": data_engineer, "data_analyst": data_analyst}
    db = SessionLocal()
    try:
        valid_ids = {u.id for u in _assignable_users(db)}
        if any(uid and uid not in valid_ids for uid in chosen.values()):
            return _new_process_page(request, user, "Utente selezionato non valido.", 400)
        # process_type e' NOT NULL nel modello: resta vuoto, non si chiede piu'
        ws = ProcessWorkspace(process_name=process_name, process_type="", created_by=user.name)
        db.add(ws)
        db.flush()
        for role, uid in chosen.items():
            if uid:
                db.add(ProcessAssignment(workspace_id=ws.id, user_id=uid, role=role, assigned_by=user.name))
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/admin", status_code=303)


@router.get("/users/new", response_class=HTMLResponse)
def new_user_form(request: Request):
    user, denied = _require_admin(request)
    if denied:
        return denied
    return templates.TemplateResponse("admin_new_user.html", {"request": request, "user": user, "error": None})


@router.post("/users/new")
def create_user(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    is_admin: bool = Form(False),
):
    user, denied = _require_admin(request)
    if denied:
        return denied

    email_norm = email.strip().lower()
    db = SessionLocal()
    try:
        if db.query(User).filter_by(email=email_norm).first():
            return templates.TemplateResponse(
                "admin_new_user.html",
                {"request": request, "user": user, "error": msg("Esiste già un utente con email {email}.", email=email_norm)},
                status_code=400,
            )
        # Qualunque admin puo' crearne un altro con gli stessi privilegi: non
        # e' un ruolo per processo (quelli sono ASSIGNABLE_ROLES via
        # ProcessAssignment), e' un flag sull'utente che da' accesso ad
        # Amministrazione (non ai moduli, che restano ai ruoli assegnati).
        db.add(User(name=name, email=email_norm, password_hash=hash_password(password), is_admin=is_admin))
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/admin", status_code=303)


@router.post("/processes/{workspace_id}/assign")
def assign_role(request: Request, workspace_id: str, user_id: str = Form(...), role: str = Form(...)):
    user, denied = _require_admin(request)
    if denied:
        return denied
    if role not in ASSIGNABLE_ROLES:
        return HTMLResponse(t(get_lang(request), "Ruolo non valido: {role}", role=role), status_code=400)

    db = SessionLocal()
    try:
        existing = db.query(ProcessAssignment).filter_by(workspace_id=workspace_id, role=role).first()
        if existing:
            existing.user_id = user_id
            existing.assigned_by = user.name
        else:
            db.add(ProcessAssignment(
                workspace_id=workspace_id, user_id=user_id, role=role, assigned_by=user.name,
            ))
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/admin", status_code=303)


@router.post("/processes/{workspace_id}/delete")
def delete_process(request: Request, workspace_id: str):
    """Elimina definitivamente un processo con tutte le sue strutture (e
    quindi anche le loro voci nel catalogo), run, file OCEL, upload e
    assegnazioni."""
    user, denied = _require_admin(request)
    if denied:
        return denied

    db = SessionLocal()
    try:
        ws = db.get(ProcessWorkspace, workspace_id)
        if ws is None:
            return RedirectResponse("/admin", status_code=303)

        files = delete_structures(db, list(workspace_config_ids(db, workspace_id)))
        # run/link residui del processo non legati a strutture ancora esistenti
        runs = db.query(ExtractionRun).filter_by(workspace_id=workspace_id).all()
        files += [Path(r.ocel_file_path) for r in runs]
        db.query(DataQualityCheckResult).filter(
            DataQualityCheckResult.extraction_run_id.in_([r.id for r in runs])
        ).delete(synchronize_session=False)
        db.query(ExtractionRun).filter_by(workspace_id=workspace_id).delete(synchronize_session=False)
        db.query(ProcessIngestionLink).filter_by(workspace_id=workspace_id).delete(synchronize_session=False)
        db.query(ProcessAssignment).filter_by(workspace_id=workspace_id).delete(synchronize_session=False)
        db.query(ProcessAssessment).filter_by(workspace_id=workspace_id).delete(synchronize_session=False)
        db.query(ProcessDocument).filter_by(workspace_id=workspace_id).delete(synchronize_session=False)
        db.query(ObjectiveCoverage).filter_by(workspace_id=workspace_id).delete(synchronize_session=False)
        db.delete(ws)
        db.commit()
    finally:
        db.close()

    remove_files([*files, DATA_DIR / "uploads" / workspace_id, DATA_DIR / "documents" / workspace_id])
    state.drop_workspace(workspace_id)

    return RedirectResponse("/admin", status_code=303)


# ---------- LLM: modello, parametri delle chiamate e chiave API (cifrata) ----------

USE_LABELS = {
    "mapping": msg("Mapping delle tabelle sorgente"),
    "relevance": msg("Controllo di pertinenza dei dati caricati"),
    "control_tower": msg("Proposta della Control Tower"),
    "assistant": msg("Assistenti, spiegazioni e traduzioni"),
}
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:@/\-]{2,120}$")


def _llm_page(request: Request, user: User, errors: list | None = None, tests: list | None = None,
              saved: bool = False, form: dict | None = None, status_code: int = 200):
    s = llm.settings()
    models = sorted(set(llm.PRICES_USD_PER_MTOK) | set(s["prices"]) | {s["uses"][u]["model"] for u in llm.USES})
    return templates.TemplateResponse(
        "admin_llm.html",
        {
            "request": request, "user": user, "s": s, "uses": llm.USES, "use_labels": USE_LABELS,
            "efforts": llm.EFFORTS, "models": models, "key_source": llm.key_source(), "errors": errors or [],
            "tests": tests, "saved": saved, "form": form,
        },
        status_code=status_code,
    )


@router.get("/llm", response_class=HTMLResponse)
def llm_settings_page(request: Request):
    user, denied = _require_admin(request)
    if denied:
        return denied
    return _llm_page(request, user)


def _num(value, cast, lo, hi, label, errors, required=True):
    value = (value or "").strip().replace(",", ".")
    if not value:
        if required:
            errors.append(msg("Valore mancante: {f}.", f=label))
        return None
    try:
        x = cast(float(value))
    except ValueError:
        errors.append(msg("Valore non valido per {f}: «{v}».", f=label, v=value))
        return None
    if not lo <= x <= hi:
        errors.append(msg("{f} deve essere tra {a} e {b}.", f=label, a=lo, b=hi))
        return None
    return x


@router.post("/llm")
async def llm_settings_save(request: Request):
    user, denied = _require_admin(request)
    if denied:
        return denied
    form = await request.form()
    errors: list = []
    enabled = form.get("enabled") == "1"
    api_key = (form.get("api_key") or "").strip()
    remove_key = form.get("remove_key") == "1"
    if api_key and (len(api_key) < 20 or any(ch.isspace() for ch in api_key)):
        errors.append(msg("La chiave API non sembra valida (troppo corta o con spazi)."))
    base_url = (form.get("base_url") or "").strip().rstrip("/")
    if base_url and not re.match(r"^https?://[^\s]+$", base_url):
        errors.append(msg("L'indirizzo dell'API deve iniziare con https:// (o http:// per un proxy locale)."))
    timeout_s = _num(form.get("timeout_s"), int, 10, 3600, "timeout", errors)
    max_retries = _num(form.get("max_retries"), int, 0, 10, "max_retries", errors)
    budget = _num(form.get("mapping_budget_usd"), float, 0.01, 100, "mapping_budget_usd", errors)
    uses = {}
    for u in llm.USES:
        model = (form.get(f"model_{u}") or "").strip()
        if not _MODEL_RE.match(model):
            errors.append(msg("Nome del modello non valido: «{v}».", v=model))
        effort = form.get(f"effort_{u}") or "low"
        if effort not in llm.EFFORTS:
            errors.append(msg("Valore non valido per {f}: «{v}».", f="effort", v=effort))
        cap = _num(form.get(f"max_tokens_{u}"), int, 256, 128000, "max_tokens", errors, required=False)
        uses[u] = {"model": model, "effort": effort, "max_tokens": cap}
    prices = {}
    for i in range(int(form.get("price_rows") or 0) + 1):
        m = (form.get(f"price_model_{i}") or "").strip()
        if not m:
            continue
        if not _MODEL_RE.match(m):
            errors.append(msg("Nome del modello non valido: «{v}».", v=m))
            continue
        pin = _num(form.get(f"price_in_{i}"), float, 0, 1000, f"{m} input", errors)
        pout = _num(form.get(f"price_out_{i}"), float, 0, 1000, f"{m} output", errors)
        if pin is not None and pout is not None:
            prices[m] = [pin, pout]
    if errors:
        return _llm_page(request, user, errors=errors, form=dict(form), status_code=400)
    llm.save(user=user.name, enabled=enabled, api_key=api_key or None, remove_key=remove_key, base_url=base_url or None,
             timeout_s=timeout_s, max_retries=max_retries, uses=uses, prices=prices, mapping_budget_usd=budget)
    tests = llm.test_connection() if form.get("action") == "test" else None
    return _llm_page(request, user, tests=tests, saved=True)


# ---------- BPA: attributi dei simboli dei BPMN e simboli senza occorrenze ----------

def _bpa_options(raw: str) -> list[dict]:
    """Voci di un elenco, una per riga: «Etichetta italiana | English label» (la seconda parte e' facoltativa)."""
    out, seen = [], set()
    for line in (raw or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if not parts or not parts[0]:
            continue
        value = parts[0][:200]
        if value.casefold() in seen:
            continue
        seen.add(value.casefold())
        out.append({"value": value, "it": parts[0][:200], "en": (parts[1] if len(parts) > 1 and parts[1] else parts[0])[:200]})
    return out[:200]


def _bpa_context(db, request, symbol_type: str, edit_key: str | None, errors=None, form=None):
    from app.models import BpaAttributeDef, BpaOccurrence, BpaSymbol
    from app.services import bpa_repo
    defs = (db.query(BpaAttributeDef).filter_by(symbol_type=symbol_type).order_by(BpaAttributeDef.position).all())
    used = {sid for (sid,) in db.query(BpaOccurrence.symbol_id).distinct()}
    counts = {}
    unused = []
    for s in db.query(BpaSymbol).order_by(BpaSymbol.symbol_type, BpaSymbol.name_it).all():
        counts[s.symbol_type] = counts.get(s.symbol_type, 0) + 1
        if s.id not in used:
            unused.append(s)
    # tabella dei simboli del tipo scelto, con i valori degli attributi
    lang = get_lang(request)
    occ_count = {}
    for (sid,) in db.query(BpaOccurrence.symbol_id).all():
        occ_count[sid] = occ_count.get(sid, 0) + 1
    rows = []
    for s in db.query(BpaSymbol).filter_by(symbol_type=symbol_type).all():
        rows.append({"id": s.id, "name": bpa_repo.display_name(db, s, lang), "anonymous": bool(s.anonymous),
                     "other": "" if s.anonymous else bpa_repo.name_of(s, "en" if lang == "it" else "it"),
                     "subtypes": ", ".join(bpa_repo.subtypes(db, s, lang)), "occ": occ_count.get(s.id, 0),
                     "values": [_bpa_value_text(d, (s.attrs or {}).get(d.key), lang) for d in defs]})
    rows.sort(key=lambda r: (r["anonymous"], r["name"].casefold()))
    return {"request": request, "types": bpa_repo.SYMBOL_TYPES, "symbol_type": symbol_type, "defs": defs,
            "data_types": bpa_repo.DATA_TYPES, "text_types": sorted(bpa_repo.TEXT_TYPES), "counts": counts,
            "unused": unused, "edit": next((d for d in defs if d.key == edit_key), None),
            "errors": errors or [], "form": form, "rows": rows}


def _bpa_value_text(d, v, lang: str) -> str:
    """Valore di un attributo come testo per la tabella dei simboli."""
    if v is None or v == "" or v == []:
        return ""
    if d.data_type == "boolean":
        return t(lang, "Sì") if v else t(lang, "No")
    labels = {o.get("value"): o.get(lang) or o.get("it") or o.get("value") for o in d.options or []}
    if d.data_type == "choice":
        return labels.get(v, str(v))
    if d.data_type == "multi_choice":
        return ", ".join(labels.get(x, str(x)) for x in (v if isinstance(v, list) else [v]))
    if isinstance(v, dict):
        return v.get(lang) or v.get("it") or v.get("en") or ""
    if d.data_type == "datetime":
        return str(v).replace("T", " ")
    return str(v)


@router.get("/bpa", response_class=HTMLResponse)
def admin_bpa(request: Request, type: str = "activity", edit: str = "", saved: int = 0):
    from app.services import bpa_repo
    user, denied = _require_admin(request)
    if denied:
        return denied
    symbol_type = type if type in bpa_repo.SYMBOL_TYPES else "activity"
    db = SessionLocal()
    try:
        ctx = _bpa_context(db, request, symbol_type, edit or None)
    finally:
        db.close()
    return templates.TemplateResponse("admin_bpa.html", {**ctx, "user": user, "saved": saved})


@router.post("/bpa/attr")
async def admin_bpa_attr_save(request: Request):
    from app.models import BpaAttributeDef
    from app.services import bpa_repo
    user, denied = _require_admin(request)
    if denied:
        return denied
    form = await request.form()
    symbol_type = form.get("symbol_type") if form.get("symbol_type") in bpa_repo.SYMBOL_TYPES else "activity"
    key = str(form.get("key") or "")
    name_it, name_en = str(form.get("name_it") or "").strip()[:200], str(form.get("name_en") or "").strip()[:200]
    data_type = str(form.get("data_type") or "")
    errors = []
    if not name_it or not name_en:
        errors.append(msg("Dai il nome dell'attributo in italiano e in inglese."))
    if name_it.casefold() in ("nome", "identificativo", "name", "id") or name_en.casefold() in ("name", "id", "identifier"):
        errors.append(msg("«Nome» e «Identificativo» sono attributi di sistema: scegli un altro nome."))
    db = SessionLocal()
    try:
        d = db.query(BpaAttributeDef).filter_by(key=key).first() if key else None
        if d is None and data_type not in bpa_repo.DATA_TYPES:
            errors.append(msg("Scegli il tipo di dato."))
        dtype = d.data_type if d is not None else data_type
        options = _bpa_options(str(form.get("options") or "")) if dtype in ("choice", "multi_choice") else None
        if dtype in ("choice", "multi_choice") and not options:
            errors.append(msg("Un elenco deve avere almeno una voce."))
        same = [x for x in db.query(BpaAttributeDef).filter_by(symbol_type=symbol_type).all()
                if x.key != key and ((x.name or {}).get("it", "").casefold() == name_it.casefold()
                                     or (x.name or {}).get("en", "").casefold() == name_en.casefold())]
        if same:
            errors.append(msg("Esiste già un attributo con questo nome per questo tipo di oggetto."))
        if errors:
            ctx = _bpa_context(db, request, symbol_type, key or None, errors, dict(form))
            return templates.TemplateResponse("admin_bpa.html", {**ctx, "user": user, "saved": 0}, status_code=400)
        if d is None:
            last = db.query(BpaAttributeDef).filter_by(symbol_type=symbol_type).count()
            d = BpaAttributeDef(symbol_type=symbol_type, key=bpa_repo.new_attr_key(), data_type=dtype, position=last)
        d.name = {"it": name_it, "en": name_en}
        hi, he = str(form.get("help_it") or "").strip()[:500], str(form.get("help_en") or "").strip()[:500]
        d.help = {"it": hi, "en": he or hi} if hi or he else None
        d.options = options
        d.required = form.get("required") == "1"
        d.translatable = dtype in bpa_repo.TEXT_TYPES   # i testi hanno sempre un valore in italiano e uno in inglese
        db.add(d)
        db.commit()
    finally:
        db.close()
    bpa_repo.refresh_views()
    return RedirectResponse(f"/admin/bpa?type={symbol_type}&saved=1", status_code=303)


@router.post("/bpa/attr/{key}/move")
def admin_bpa_attr_move(request: Request, key: str, dir: str = Form("up")):
    from app.models import BpaAttributeDef
    from app.services import bpa_repo
    user, denied = _require_admin(request)
    if denied:
        return denied
    db = SessionLocal()
    try:
        d = db.query(BpaAttributeDef).filter_by(key=key).first()
        if d is None:
            return RedirectResponse("/admin/bpa", status_code=303)
        defs = db.query(BpaAttributeDef).filter_by(symbol_type=d.symbol_type).order_by(BpaAttributeDef.position).all()
        i = defs.index(d)
        j = i - 1 if dir == "up" else i + 1
        if 0 <= j < len(defs):
            defs[i], defs[j] = defs[j], defs[i]
        for n, x in enumerate(defs):
            x.position = n
            db.add(x)
        db.commit()
        symbol_type = d.symbol_type
    finally:
        db.close()
    bpa_repo.refresh_views()
    return RedirectResponse(f"/admin/bpa?type={symbol_type}", status_code=303)


@router.post("/bpa/attr/{key}/delete")
def admin_bpa_attr_delete(request: Request, key: str):
    """Toglie l'attributo e i suoi valori da tutti i simboli."""
    from app.models import BpaAttributeDef, BpaSymbol
    from app.services import bpa_repo
    user, denied = _require_admin(request)
    if denied:
        return denied
    db = SessionLocal()
    try:
        d = db.query(BpaAttributeDef).filter_by(key=key).first()
        if d is None:
            return RedirectResponse("/admin/bpa", status_code=303)
        symbol_type = d.symbol_type
        for s in db.query(BpaSymbol).filter_by(symbol_type=symbol_type).all():
            if key in (s.attrs or {}):
                s.attrs = {k: v for k, v in s.attrs.items() if k != key}
                db.add(s)
        db.delete(d)
        db.commit()
    finally:
        db.close()
    bpa_repo.refresh_views()
    return RedirectResponse(f"/admin/bpa?type={symbol_type}&saved=1", status_code=303)


@router.post("/bpa/unused/delete")
async def admin_bpa_unused_delete(request: Request):
    """Elimina i simboli scelti che non compaiono in nessun BPMN."""
    from app.models import BpaOccurrence, BpaRelation, BpaSymbol
    user, denied = _require_admin(request)
    if denied:
        return denied
    form = await request.form()
    ids = [str(x) for x in form.getlist("symbol")]
    db = SessionLocal()
    try:
        for sid in ids:
            if db.query(BpaOccurrence).filter_by(symbol_id=sid).first() is not None:
                continue   # nel frattempo e' stato usato in un BPMN: resta
            db.query(BpaRelation).filter((BpaRelation.source_id == sid) | (BpaRelation.target_id == sid)).delete(synchronize_session=False)
            s = db.get(BpaSymbol, sid)
            if s is not None:
                db.delete(s)
        db.commit()
    finally:
        db.close()
    return RedirectResponse(f"/admin/bpa?type={form.get('symbol_type') or 'activity'}&saved=1#unused", status_code=303)


@router.get("/bpa/symbols/{symbol_id}")
def admin_bpa_symbol(request: Request, symbol_id: str):
    """Pannello delle proprieta' di un simbolo (come nell'editor BPMN)."""
    from fastapi.responses import JSONResponse
    from app.routers.bpa import symbol_detail
    user, denied = _require_admin(request)
    if denied:
        return JSONResponse({"error": t(get_lang(request), "Accesso negato.")}, status_code=403)
    lang = get_lang(request)
    db = SessionLocal()
    try:
        d = symbol_detail(db, symbol_id, lang, lang, user)
        return JSONResponse(d or {"error": t(lang, "Oggetto non trovato.")}, status_code=200 if d else 404)
    finally:
        db.close()


@router.post("/bpa/symbols/{symbol_id}/attrs")
async def admin_bpa_symbol_attrs(request: Request, symbol_id: str):
    from fastapi.responses import JSONResponse
    from app.routers.bpa import save_symbol_attrs
    user, denied = _require_admin(request)
    if denied:
        return JSONResponse({"error": t(get_lang(request), "Accesso negato.")}, status_code=403)
    body = await request.json()
    ui = get_lang(request)
    lang = body.get("lang") if body.get("lang") in ("it", "en") else ui
    values = body.get("values") if isinstance(body.get("values"), dict) else {}
    db = SessionLocal()
    try:
        out = save_symbol_attrs(db, symbol_id, values, lang, ui, user.name)
        return JSONResponse(out or {"error": t(ui, "Oggetto non trovato.")}, status_code=200 if out else 404)
    finally:
        db.close()
