import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from typing import Optional
import os
import csv
import io
import asyncio
import uuid
import re
import json
from datetime import date, datetime
from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, Response, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pdf_generator import generate_flood_certificate_pdf, generate_borrower_notice_pdf, generate_batch_report_pdf
from fema_lookup import (
    geocode_address, query_fema_nfhl, query_nfip_community, query_firm_panel,
    query_county_name, query_nfip_community_csb, query_tigerweb_fips,
    determine_flood_info, nfip_community_info, check_loma_at_point,
    ZONE_DISPLAY_NAMES,
)
from map_utils import generate_map_image
from db import (
    init_db, save_determination, get_determination,
    search_determinations, list_determinations, list_determinations_admin,
    delete_determination, bulk_delete_determinations,
    set_life_of_loan, flag_redetermination, list_monitored, count_flagged,
    init_auth_tables, create_user, get_user_by_email, get_user_by_id,
    list_users_by_status, list_users_by_company,
    approve_user, reject_user, update_user_password, set_user_status, delete_user,
    count_pending_users, set_user_company,
    get_user_by_reset_token, set_reset_token, clear_reset_token,
    init_companies, get_or_create_company, get_company_by_id, list_companies,
    init_audit_tables, log_admin_deletion, list_admin_deletion_log,
    init_lol_tables, init_loma_table, upsert_lol_monitoring, get_lol_monitoring,
    list_lol_monitoring, list_active_lol_monitoring,
    close_lol_monitoring, update_lol_last_checked,
    create_lol_alert, list_lol_alerts, count_failed_lol_alerts,
    ADMIN_COMPANY_ID, ADMIN_COMPANY_NAME,
)
from email_sender import (
    send_certificate_email, send_redetermination_notification,
    send_access_request_confirmation, send_admin_access_notification,
    send_welcome_email, send_rejection_email, send_lol_alert_email,
    send_password_reset_email,
)
from auth import (
    SUPER_ADMIN_EMAIL, SECRET_KEY, is_public,
    hash_password, verify_password, generate_temp_password, get_session_user,
)

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    _scheduler = AsyncIOScheduler()
    _SCHEDULER_AVAILABLE = True
except ImportError:
    _SCHEDULER_AVAILABLE = False
    _scheduler = None

def _cert_filename(flood_zone: str, property_address: str) -> str:
    """Build a clean PDF filename: Flood Cert - {zone label} - {street}.pdf"""
    raw_zone = (flood_zone or "").strip().upper()
    zone_label = ZONE_DISPLAY_NAMES.get(raw_zone, f"Zone {raw_zone}" if raw_zone else "Unknown")
    addr = (property_address or "Unknown").strip()
    street = addr.split(",")[0].strip() if addr else "Unknown"
    street_clean = re.sub(r'[\\/:*?"<>|]', "", street)
    street_clean = re.sub(r"\s+", " ", street_clean).strip()
    if len(street_clean) > 80:
        street_clean = street_clean[:80].rstrip()
    return f"Flood Cert - {zone_label} - {street_clean}.pdf"


US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA",
    "HI","ID","IL","IN","IA","KS","KY","LA","ME","MD",
    "MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC",
    "SD","TN","TX","UT","VT","VA","WA","WV","WI","WY",
    "DC","PR","GU","VI",
}

_batch_results: dict[str, bytes] = {}
_batch_record_ids: dict[str, list[int]] = {}
_batch_full_results: dict[str, list] = {}


def parse_state_zip(address: str) -> tuple[str, str] | None:
    m = re.search(r'\b([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\s*$', address.strip())
    return (m.group(1).upper(), m.group(2)) if m else None


def validate_us_address(state: str, zipcode: str) -> None:
    if state.upper() not in US_STATES:
        raise HTTPException(400, detail="Only US addresses supported")
    if not re.match(r"^\d{5}(-\d{4})?$", zipcode):
        raise HTTPException(400, detail="Invalid US ZIP code format")


app = FastAPI(title="FEMA Flood Certificate Generator")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


# ── Auth middleware ───────────────────────────────────────────────────────────

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if is_public(request.url.path):
            return await call_next(request)
        user = request.session.get("user")
        if not user:
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)


app.add_middleware(AuthMiddleware)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=60 * 60 * 8)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_admin(request: Request) -> dict:
    user = get_session_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _session_company_id(request: Request) -> int | None:
    user = get_session_user(request)
    if not user:
        return None
    if user.get("is_admin"):
        return None
    return user.get("company_id")


# ── LOL monitoring core check logic ──────────────────────────────────────────

_LOL_CHECKS = [
    ("baseline_panel_number",     "NFIP Map Panel Number",         "panel_number"),
    ("baseline_effective_date",   "FIRM Panel Effective Date",     "panel_effective_date"),
    ("baseline_flood_zone",       "Flood Zone",                    "flood_zone"),
    ("baseline_community_number", "NFIP Community Number (CID)",   "community_number"),
]


async def _check_lol_record(mon: dict) -> None:
    lat, lon = mon.get("lat"), mon.get("lon")
    if not lat or not lon:
        return
    try:
        zone_data, community_data, firm_data = await asyncio.gather(
            query_fema_nfhl(float(lat), float(lon)),
            query_nfip_community(float(lat), float(lon)),
            query_firm_panel(float(lat), float(lon)),
        )
        new_info = determine_flood_info({**zone_data, **community_data, **firm_data})
    except Exception as exc:
        print(f"[LOL] FEMA query failed for monitoring_id={mon['id']}: {exc}")
        return

    update_lol_last_checked(mon["id"])

    changed_items = []
    for baseline_key, label, new_key in _LOL_CHECKS:
        old_val = mon.get(baseline_key, "")
        new_val = new_info.get(new_key, "")
        if str(old_val) != str(new_val):
            changed_items.append({
                "field": baseline_key,
                "label": label,
                "old_value": old_val,
                "new_value": new_val,
            })

    if not changed_items:
        return

    old_vals = {c["field"]: c["old_value"] for c in changed_items}
    new_vals = {c["field"]: c["new_value"] for c in changed_items}
    field_names = [c["field"] for c in changed_items]

    lender_email = (mon.get("lender_email") or "").strip()
    email_status = "Sent"

    if lender_email:
        last_exc = None
        for attempt in range(3):
            try:
                send_lol_alert_email(lender_email, mon, changed_items)
                break
            except Exception as e:
                last_exc = e
                await asyncio.sleep(2)
        else:
            email_status = "Failed"
            print(f"[LOL] Alert email failed after 3 attempts for monitoring_id={mon['id']}: {last_exc}")
    else:
        email_status = "Failed"

    create_lol_alert(
        monitoring_id=mon["id"],
        changed_fields=field_names,
        old_values=old_vals,
        new_values=new_vals,
        lender_email=lender_email,
        email_status=email_status,
    )


async def run_lol_daily_check():
    records = list_active_lol_monitoring()
    if not records:
        return
    print(f"[LOL] Daily check started for {len(records)} active records")
    sem = asyncio.Semaphore(3)

    async def bounded(mon):
        async with sem:
            await _check_lol_record(mon)

    await asyncio.gather(*[bounded(m) for m in records])
    print("[LOL] Daily check complete")


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
    init_companies()
    init_auth_tables()
    init_audit_tables()
    init_lol_tables()
    init_loma_table()

    admin_pw = os.getenv("ADMIN_PASSWORD", "")
    existing_admin = get_user_by_email(SUPER_ADMIN_EMAIL)
    if admin_pw:
        if not existing_admin:
            create_user(
                email=SUPER_ADMIN_EMAIL,
                name="Rishu Kannihalli",
                first_name="Rishu",
                last_name="Kannihalli",
                company_id=ADMIN_COMPANY_ID,
                status="active",
                is_admin=1,
                password_hash=hash_password(admin_pw),
            )
            print(f"[AUTH] Super admin created for {SUPER_ADMIN_EMAIL}")
        else:
            update_user_password(existing_admin["id"], hash_password(admin_pw))
            if not existing_admin.get("company_id"):
                set_user_company(existing_admin["id"], ADMIN_COMPANY_ID)
    else:
        if not existing_admin:
            tmp = generate_temp_password()
            create_user(
                email=SUPER_ADMIN_EMAIL,
                name="Rishu Kannihalli",
                first_name="Rishu",
                last_name="Kannihalli",
                company_id=ADMIN_COMPANY_ID,
                status="active",
                is_admin=1,
                password_hash=hash_password(tmp),
            )
            print(f"[AUTH] Super admin created with temp password: {tmp}")
        else:
            if not existing_admin.get("company_id"):
                set_user_company(existing_admin["id"], ADMIN_COMPANY_ID)

    if _SCHEDULER_AVAILABLE and _scheduler:
        try:
            _scheduler.add_job(run_lol_daily_check, "cron", hour=6, minute=0, id="lol_daily")
            _scheduler.add_job(_rebuild_nfip_db_background, "cron", hour=5, minute=0, id="nfip_daily")
            _scheduler.start()
            print("[LOL] Daily scheduler started (runs at 06:00 UTC)")
        except Exception as e:
            print(f"[LOL] Scheduler startup error: {e}")


async def _rebuild_nfip_db_background():
    import json as _json, os as _os, httpx as _httpx
    from fema_lookup import STATE_FIPS as _SF
    url = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/22/query"
    db = {}
    offset = 0
    total = 0
    try:
        async with _httpx.AsyncClient(timeout=30.0) as client:
            for _ in range(100):
                params = {
                    "where": "1=1",
                    "outFields": "POL_NAME1,CID,DFIRM_ID",
                    "returnGeometry": "false",
                    "resultRecordCount": "1000",
                    "resultOffset": str(offset),
                    "orderByFields": "OBJECTID",
                    "f": "json",
                }
                r = await client.get(url, params=params)
                r.raise_for_status()
                features = r.json().get("features", [])
                if not features:
                    break
                for feat in features:
                    a = feat.get("attributes", {})
                    cid = (a.get("CID") or "").strip()
                    name = (a.get("POL_NAME1") or "").strip()
                    dfirm = (a.get("DFIRM_ID") or "").strip()
                    if not (cid and name):
                        continue
                    sfips = cid[:2] if len(cid) >= 5 else (dfirm[:2] if len(dfirm) >= 2 else "")
                    cfips = cid[2:5] if len(cid) >= 5 else (dfirm[2:5] if len(dfirm) >= 5 else "")
                    state_abbr = _SF.get(sfips, ("", ""))[1] if sfips else ""
                    if not (state_abbr and cfips):
                        continue
                    key = f"{state_abbr}_{cfips}"
                    entry = {"cid": cid, "name": name}
                    if key not in db:
                        db[key] = []
                    if not any(e["cid"] == cid and e["name"] == name for e in db[key]):
                        db[key].append(entry)
                total += len(features)
                if len(features) < 1000:
                    break
                offset += 1000
        outpath = _os.path.join(_os.path.dirname(__file__), "nfip_communities_db.json")
        with open(outpath, "w") as fp:
            _json.dump(db, fp, separators=(",", ":"))
        import fema_lookup as _fl
        _fl._NFIP_DB = {}
        print(f"[NFIP] Daily DB rebuild complete: {len(db)} county keys, {total} features")
    except Exception as e:
        print(f"[NFIP] Daily DB rebuild failed: {e}")


@app.on_event("shutdown")
async def shutdown():
    if _SCHEDULER_AVAILABLE and _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if get_session_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html")


@app.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    user = get_user_by_email(email)
    if not user:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Invalid email or password.", "email": email},
        )
    if user["status"] == "pending":
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Your access request is pending approval.", "email": email},
        )
    if user["status"] in ("rejected", "inactive"):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Your account has been deactivated. Contact the administrator.", "email": email},
        )
    if not user.get("password_hash") or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Invalid email or password.", "email": email},
        )

    company_id = user.get("company_id")
    if user.get("is_admin") and not company_id:
        company_id = ADMIN_COMPANY_ID

    display_name = (
        user.get("name")
        or f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        or user["email"]
    )
    request.session["user"] = {
        "id": user["id"],
        "email": user["email"],
        "name": display_name,
        "is_admin": bool(user["is_admin"]),
        "company_id": company_id,
        "company_name": user.get("company_name") or "",
    }
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/request-access", response_class=HTMLResponse)
async def request_access_get(request: Request):
    return templates.TemplateResponse(request, "request_access.html")


@app.post("/request-access", response_class=HTMLResponse)
async def request_access_post(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    company_name: str = Form(...),
    company_address: str = Form(...),
    email: str = Form(...),
    contact_number: str = Form(...),
):
    first_name      = first_name.strip()
    last_name       = last_name.strip()
    company_name    = company_name.strip()
    company_address = company_address.strip()
    email           = email.strip().lower()
    contact_number  = contact_number.strip()

    errors = []
    if not all([first_name, last_name, company_name, company_address, email, contact_number]):
        errors.append("All fields are required.")

    if email and not re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email):
        errors.append("Please enter a valid email address.")

    digits_only = re.sub(r'\D', '', contact_number)
    if len(digits_only) != 10:
        errors.append("Contact number must be exactly 10 digits (US format).")
    else:
        contact_number = f"({digits_only[:3]}) {digits_only[3:6]}-{digits_only[6:]}"

    form_data = {
        "first_name": first_name, "last_name": last_name,
        "company_name": company_name, "company_address": company_address,
        "email": email, "contact_number": contact_number,
    }

    if errors:
        return templates.TemplateResponse(
            request, "request_access.html",
            {"errors": errors, "form": form_data},
        )

    existing = get_user_by_email(email)
    if existing:
        if existing["status"] == "active":
            return templates.TemplateResponse(
                request, "request_access.html",
                {"errors": ["An account with this email already exists. Please sign in."], "form": form_data},
            )
        return templates.TemplateResponse(request, "request_access.html", {"submitted": True})

    create_user(
        email=email,
        first_name=first_name,
        last_name=last_name,
        company_name=company_name,
        company_address=company_address,
        contact_number=contact_number,
        status="pending",
    )

    user_data = {
        "first_name": first_name, "last_name": last_name,
        "company_name": company_name, "company_address": company_address,
        "email": email, "contact_number": contact_number,
        "created_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }
    try:
        send_access_request_confirmation(email, user_data)
    except Exception as e:
        print(f"[REQUEST-ACCESS] Confirmation email failed: {e}")
    try:
        send_admin_access_notification(SUPER_ADMIN_EMAIL, user_data)
    except Exception as e:
        print(f"[REQUEST-ACCESS] Admin notification email failed: {e}")

    return templates.TemplateResponse(request, "request_access.html", {"submitted": True})


# ── Admin routes ──────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(
    request: Request,
    tab: str = "requests",
    cid: Optional[int] = None,
    uid: Optional[str] = None,
    dfrom: str = "",
    dto: str = "",
    hq: str = "",
    zone: str = "",
    req_status: str = "",
):
    _require_admin(request)
    flash = request.session.pop("flash_approval", None)

    all_users = list_users_by_status()
    pending  = [u for u in all_users if u["status"] == "pending"]
    active   = [u for u in all_users if u["status"] == "active"]
    inactive = [u for u in all_users if u["status"] in ("inactive", "rejected")]

    status_scope = req_status if req_status in ("pending", "active", "rejected", "inactive") else None
    access_requests = list_users_by_status(status=status_scope)

    for u in access_requests:
        if not u.get("name") or u["name"] == "":
            u["display_name"] = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or u["email"]
        else:
            u["display_name"] = u["name"]
    for u in all_users + pending + active + inactive:
        if not u.get("name") or u["name"] == "":
            u["display_name"] = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or u["email"]
        else:
            u["display_name"] = u["name"]

    companies = list_companies()
    history_records = []
    history_users = []
    selected_company = None
    uid_int = int(uid) if uid and str(uid).strip().isdigit() else None
    if tab == "companies" and cid:
        history_records = list_determinations_admin(
            company_id=cid,
            user_id=uid_int,
            date_from=dfrom or None,
            date_to=dto or None,
            query=hq or None,
            flood_zone=zone or None,
        )
        history_users = list_users_by_company(cid)
        selected_company = get_company_by_id(cid)

    lol_alerts = list_lol_alerts() if tab == "lol" else []

    stats = {
        "total": len(all_users),
        "pending": len(pending),
        "active": len(active),
        "rejected": len(inactive),
        "failed_alerts": count_failed_lol_alerts(),
    }

    return templates.TemplateResponse(request, "admin.html", {
        "tab": tab,
        "pending": pending, "active": active, "inactive": inactive,
        "stats": stats, "flash": flash,
        "access_requests": access_requests,
        "req_status": req_status,
        "companies": companies,
        "selected_company": selected_company,
        "cid": cid, "uid": uid,
        "history_records": history_records,
        "history_users": history_users,
        "filter_dfrom": dfrom, "filter_dto": dto,
        "filter_hq": hq, "filter_zone": zone,
        "lol_alerts": lol_alerts,
        "current_user": get_session_user(request),
    })


@app.post("/admin/rebuild-nfip-db")
async def rebuild_nfip_db(request: Request):
    _require_admin(request)
    import json as _json, os as _os, httpx as _httpx
    from fema_lookup import STATE_FIPS as _SF
    url = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/22/query"
    db = {}
    offset = 0
    page_size = 1000
    total_fetched = 0
    max_pages = 100
    try:
        async with _httpx.AsyncClient(timeout=30.0) as client:
            for page in range(max_pages):
                params = {
                    "where": "1=1",
                    "outFields": "POL_NAME1,CID,DFIRM_ID",
                    "returnGeometry": "false",
                    "resultRecordCount": str(page_size),
                    "resultOffset": str(offset),
                    "orderByFields": "OBJECTID",
                    "f": "json",
                }
                r = await client.get(url, params=params)
                r.raise_for_status()
                data = r.json()
                if "error" in data:
                    return JSONResponse({"status": "error", "detail": str(data["error"])[:300], "fetched_so_far": total_fetched}, status_code=502)
                features = data.get("features", [])
                if not features:
                    break
                for feat in features:
                    a = feat.get("attributes", {})
                    cid = (a.get("CID") or "").strip()
                    name = (a.get("POL_NAME1") or "").strip()
                    dfirm = (a.get("DFIRM_ID") or "").strip()
                    if not (cid and name):
                        continue
                    sfips = cid[:2] if len(cid) >= 5 else (dfirm[:2] if len(dfirm) >= 2 else "")
                    cfips = cid[2:5] if len(cid) >= 5 else (dfirm[2:5] if len(dfirm) >= 5 else "")
                    state_abbr = _SF.get(sfips, ("", ""))[1] if sfips else ""
                    if not (state_abbr and cfips):
                        continue
                    key = f"{state_abbr}_{cfips}"
                    entry = {"cid": cid, "name": name}
                    if key not in db:
                        db[key] = []
                    if not any(e["cid"] == cid and e["name"] == name for e in db[key]):
                        db[key].append(entry)
                total_fetched += len(features)
                if len(features) < page_size:
                    break
                offset += page_size
        outpath = _os.path.join(_os.path.dirname(__file__), "nfip_communities_db.json")
        with open(outpath, "w") as fp:
            _json.dump(db, fp, separators=(",", ":"))
        import fema_lookup as _fl
        _fl._NFIP_DB = {}
        return JSONResponse({"status": "ok", "county_keys": len(db), "features_fetched": total_fetched})
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)[:500], "fetched_so_far": total_fetched}, status_code=500)


@app.post("/admin/users/{user_id}/approve")
async def admin_approve(request: Request, user_id: int):
    _require_admin(request)
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404)

    company_name = (user.get("company_name") or "").strip()
    company_address = (user.get("company_address") or "").strip()
    if company_name:
        company_id = get_or_create_company(company_name, company_address)
    else:
        company_id = ADMIN_COMPANY_ID

    set_user_company(user_id, company_id)
    tmp_pw = generate_temp_password()
    approve_user(user_id, hash_password(tmp_pw))

    display_name = (
        f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        or user.get("name") or user["email"]
    )

    try:
        send_welcome_email(
            to_email=user["email"],
            name=display_name,
            company_name=company_name or ADMIN_COMPANY_NAME,
            temp_password=tmp_pw,
        )
    except Exception as e:
        print(f"[ADMIN] Welcome email failed: {e}")

    request.session["flash_approval"] = {
        "name": display_name,
        "email": user["email"],
        "password": tmp_pw,
    }
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/{user_id}/reject")
async def admin_reject(request: Request, user_id: int):
    _require_admin(request)
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404)
    reject_user(user_id)
    try:
        display_name = (
            f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
            or user.get("name") or ""
        )
        send_rejection_email(user["email"], display_name)
    except Exception as e:
        print(f"[ADMIN] Rejection email failed: {e}")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/{user_id}/deactivate")
async def admin_deactivate(request: Request, user_id: int):
    _require_admin(request)
    user = get_user_by_id(user_id)
    if user and user.get("is_admin"):
        raise HTTPException(400, detail="Cannot deactivate an admin account")
    set_user_status(user_id, "inactive")
    return RedirectResponse("/admin?tab=users", status_code=303)


@app.post("/admin/users/{user_id}/reactivate")
async def admin_reactivate(request: Request, user_id: int):
    _require_admin(request)
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404)
    set_user_status(user_id, "active")
    return RedirectResponse("/admin?tab=users", status_code=303)


@app.post("/admin/users/{user_id}/reset-password")
async def admin_reset_password(request: Request, user_id: int):
    _require_admin(request)
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404)
    tmp_pw = generate_temp_password()
    update_user_password(user_id, hash_password(tmp_pw))
    display_name = (
        f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        or user.get("name") or user["email"]
    )
    request.session["flash_approval"] = {
        "name": display_name,
        "email": user["email"],
        "password": tmp_pw,
    }
    return RedirectResponse("/admin?tab=users", status_code=303)


@app.post("/admin/users/{user_id}/delete")
async def admin_delete_user(request: Request, user_id: int):
    _require_admin(request)
    user = get_user_by_id(user_id)
    if user and user.get("is_admin"):
        raise HTTPException(400, detail="Cannot delete the admin account")
    delete_user(user_id)
    return RedirectResponse("/admin?tab=users", status_code=303)


@app.post("/admin/users/create")
async def admin_create_user(
    request: Request,
    first_name: str = Form(default=""),
    last_name: str = Form(default=""),
    name: str = Form(default=""),
    email: str = Form(...),
    password: str = Form(default=""),
    company_name: str = Form(default=""),
):
    _require_admin(request)
    email = email.strip().lower()
    tmp_pw = password.strip() if password.strip() else generate_temp_password()
    existing = get_user_by_email(email)
    if existing:
        return RedirectResponse("/admin?tab=users", status_code=303)

    display_name = name.strip() or f"{first_name.strip()} {last_name.strip()}".strip() or email
    company_id = None
    if company_name.strip():
        company_id = get_or_create_company(company_name.strip())

    create_user(
        email=email,
        name=display_name,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        company_name=company_name.strip(),
        company_id=company_id,
        status="active",
        password_hash=hash_password(tmp_pw),
    )
    request.session["flash_approval"] = {"name": display_name, "email": email, "password": tmp_pw}
    return RedirectResponse("/admin?tab=users", status_code=303)


@app.post("/admin/history/{record_id}/delete")
async def admin_delete_history(
    request: Request,
    record_id: int,
    reason: str = Form(default=""),
):
    admin_user = _require_admin(request)
    record = get_determination(record_id)
    if not record:
        raise HTTPException(404)
    log_admin_deletion(admin_user["id"], [record_id], reason)
    delete_determination(record_id)
    company_id = record.get("company_id") or 0
    return RedirectResponse(f"/admin?tab=companies&cid={company_id}", status_code=303)


@app.post("/admin/history/bulk-delete")
async def admin_bulk_delete(request: Request):
    admin_user = _require_admin(request)
    form = await request.form()
    ids_raw = form.getlist("record_ids")
    reason = form.get("reason", "")
    cid_back = form.get("cid", "0")
    try:
        record_ids = [int(x) for x in ids_raw if x]
    except ValueError:
        raise HTTPException(400, "Invalid record IDs")
    if record_ids:
        log_admin_deletion(admin_user["id"], record_ids, reason)
        bulk_delete_determinations(record_ids)
    return RedirectResponse(f"/admin?tab=companies&cid={cid_back}", status_code=303)


@app.post("/admin/lol/run-check")
async def admin_run_lol_check(request: Request):
    _require_admin(request)
    asyncio.create_task(run_lol_daily_check())
    return RedirectResponse("/admin?tab=lol", status_code=303)


@app.post("/admin/live-test")
async def admin_live_test(request: Request):
    _require_admin(request)
    body = await request.json()
    address = (body.get("address") or "").strip()
    if not address:
        return JSONResponse({"error": "address required"}, status_code=400)

    try:
        geo = await geocode_address(address)
        if not geo:
            return JSONResponse({
                "address": address,
                "status": "undetermined",
                "note": "All geocoders failed or returned implausible results for this address.",
            })

        lat, lon = float(geo["lat"]), float(geo["lon"])

        nfhl, panel_data, community_data = await asyncio.gather(
            query_fema_nfhl(lat, lon),
            query_firm_panel(lat, lon, county_fips=geo.get("state_fips","") + geo.get("county_fips","")),
            query_nfip_community(lat, lon),
        )

        from fema_lookup import _classify_x_zone, ZONE_DISPLAY_NAMES
        raw_zone = (nfhl.get("flood_zone") or "X").upper().strip()
        sub = nfhl.get("zone_subtype") or ""
        flood_zone = _classify_x_zone(raw_zone, sub) if raw_zone == "X" else raw_zone

        return JSONResponse({
            "address": address,
            "status": "ok",
            "geocoded_address": geo.get("matched_address", ""),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "county": geo.get("county_name", ""),
            "flood_zone": flood_zone,
            "zone_label": ZONE_DISPLAY_NAMES.get(flood_zone, flood_zone),
            "in_sfha": nfhl.get("in_sfha", False),
            "zone_subtype": sub,
            "firm_panel": panel_data.get("firm_panel_l3", ""),
            "eff_date": panel_data.get("eff_date", ""),
            "community_id": community_data.get("community_id", ""),
            "community_name": community_data.get("community_name", ""),
        })
    except Exception as exc:
        return JSONResponse(
            {"address": address, "status": "error", "error": str(exc)},
            status_code=500,
        )


# ── App routes ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = get_session_user(request)
    pending_count = count_pending_users() if user and user.get("is_admin") else 0
    return templates.TemplateResponse(request, "index.html", {
        "current_user": user,
        "pending_count": pending_count,
    })


@app.post("/geocode")
async def geocode_endpoint(request: Request):
    body = await request.json()
    address = (body.get("address") or "").strip()
    if not address:
        return JSONResponse({"error": "address required"}, status_code=400)
    result = await geocode_address(address)
    if not result:
        return JSONResponse({"error": "Could not geocode address"}, status_code=422)
    return JSONResponse(result)


@app.post("/generate", response_class=HTMLResponse)
async def generate(
    request: Request,
    property_address: str = Form(...),
    loan_id: str = Form(...),
    borrower_name: str = Form(...),
    lender_name: str = Form(...),
    lender_address: str = Form(default=""),
    lender_email: str = Form(default=""),
    matched_address: str = Form(default=""),
    lat: str = Form(default=""),
    lon: str = Form(default=""),
    flood_zone: str = Form(default=""),
    flood_zone_description: str = Form(default=""),
    sfha_status: str = Form(default=""),
    insurance_required: str = Form(default=""),
    panel_number: str = Form(default=""),
    panel_effective_date: str = Form(default=""),
    community_number: str = Form(default=""),
    community_name: str = Form(default=""),
):
    errors = []
    if not property_address.strip():
        errors.append("Property address is required.")
    if not loan_id.strip():
        errors.append("Loan ID is required.")
    if not borrower_name.strip():
        errors.append("Borrower name is required.")
    if not lender_name.strip():
        errors.append("Lender name is required.")

    if property_address.strip():
        parsed = parse_state_zip(property_address)
        if parsed:
            state, zipcode = parsed
            try:
                validate_us_address(state, zipcode)
            except HTTPException as exc:
                errors.append(exc.detail)

    session_user = get_session_user(request)

    if errors:
        return templates.TemplateResponse(request, "index.html", {
            "errors": errors,
            "current_user": session_user, "pending_count": 0,
            "form": {
                "property_address": property_address, "loan_id": loan_id,
                "borrower_name": borrower_name, "lender_name": lender_name,
                "lender_address": lender_address, "lender_email": lender_email,
            }
        })

    geo_result = await geocode_address(property_address)
    if not geo_result and lat.strip() and lon.strip():
        geo_result = {
            "lat": float(lat), "lon": float(lon),
            "matched_address": matched_address or property_address,
            "city": "", "state_abbr": "", "state_fips": "", "county_fips": "", "county_name": ""
        }
    if not geo_result:
        return templates.TemplateResponse(request, "index.html", {
            "errors": ["Could not geocode the provided address. Please check the address and try again."],
            "form": {
                "property_address": property_address, "loan_id": loan_id,
                "borrower_name": borrower_name, "lender_name": lender_name,
                "lender_email": lender_email,
            }
        })

    # Step 1: Get authoritative FIPS from TIGERweb
    zone_data, community_data, county_data, tiger_data = await asyncio.gather(
        query_fema_nfhl(geo_result["lat"], geo_result["lon"]),
        query_nfip_community(geo_result["lat"], geo_result["lon"]),
        query_county_name(geo_result["lat"], geo_result["lon"]),
        query_tigerweb_fips(geo_result["lat"], geo_result["lon"]),
    )
    if tiger_data.get("state_fips"):
        geo_result["state_fips"]  = tiger_data["state_fips"]
        geo_result["county_fips"] = tiger_data["county_fips"]
        if not geo_result.get("county_name"):
            geo_result["county_name"] = tiger_data.get("county_name", "")
        print(f"TIGERweb FIPS: {tiger_data['state_fips']}{tiger_data['county_fips']} ({tiger_data.get('county_name','')})")
    elif not geo_result.get("state_fips") or not geo_result.get("county_fips"):
        print("Warning: no FIPS from Census or TIGERweb")

    # Step 2: FIRM panel + CSB with authoritative FIPS
    firm_data, csb_data = await asyncio.gather(
        query_firm_panel(
            geo_result["lat"], geo_result["lon"],
            county_fips=geo_result.get("state_fips","") + geo_result.get("county_fips","")
        ),
        query_nfip_community_csb(
            geo_result.get("state_fips", ""),
            geo_result.get("county_fips", ""),
            geo_result.get("city", ""),
            geo_result.get("state_abbr", ""),
        ),
    )
    flood_info = determine_flood_info({
        **zone_data, **community_data, **firm_data, **county_data, **csb_data,
        "geocoded_city": geo_result.get("city", ""),
        "state_fips": geo_result.get("state_fips", ""),
        "county_fips": geo_result.get("county_fips", ""),
        "county_name": geo_result.get("county_name", ""),
    })

    geo_lat = geo_result["lat"]
    geo_lon = geo_result["lon"]
    geo_matched = geo_result.get("matched_address", property_address)

    # Step 3: Check for LOMA/LOMR — overrides NFHL zone if effective removal found
    loma = await check_loma_at_point(geo_lat, geo_lon)
    loma_note = None
    loma_original_zone = None
    if loma and loma.get("status") == "Effective" and loma.get("outcome_zone") in ("X", "X500", "X (Shaded)"):
        loma_original_zone = flood_info["flood_zone"]
        flood_info["flood_zone"] = loma.get("outcome_zone", "X")
        flood_info["sfha_status"] = "No"
        flood_info["insurance_required"] = "No"
        loma_note = (
            f"Removed from SFHA per FEMA {loma['amendment_type']} "
            f"Case No. {loma['case_number']} "
            f"(effective {loma['effective_date']}). "
            f"Map shows {loma_original_zone} — LOMA overrides."
        )
        print(f"[LOMA] Override applied: {loma['case_number']} at ({geo_lat},{geo_lon})")

    # Determine company_id and user_id from session
    s_company_id = session_user.get("company_id") if session_user else None
    s_user_id = session_user.get("id") if session_user else None
    if session_user and session_user.get("is_admin") and not s_company_id:
        s_company_id = ADMIN_COMPANY_ID

    certificate_data = {
        "property_address": property_address,
        "matched_address": geo_matched,
        "loan_id": loan_id,
        "borrower_name": borrower_name,
        "lender_name": lender_name,
        "lender_address": lender_address.strip(),
        "lender_email": lender_email.strip(),
        "lat": geo_lat, "lon": geo_lon,
        "flood_zone": flood_info["flood_zone"],
        "flood_zone_description": flood_info["flood_zone_description"],
        "sfha_status": flood_info["sfha_status"],
        "insurance_required": flood_info["insurance_required"],
        "panel_number": flood_info["panel_number"],
        "panel_effective_date": flood_info["panel_effective_date"],
        "community_number": flood_info["community_number"],
        "community_name": flood_info["community_name"],
        "county": flood_info.get("county", ""),
        "determination_date": date.today().strftime("%B %d, %Y"),
        "determination_date_iso": date.today().isoformat(),
        "company_id": s_company_id,
        "user_id": s_user_id,
        "loma_case_number":    loma.get("case_number")    if loma else None,
        "loma_amendment_type": loma.get("amendment_type") if loma else None,
        "loma_effective_date": loma.get("effective_date") if loma else None,
        "loma_original_zone":  loma_original_zone,
        "loma_note":           loma_note,
    }

    record_id = save_determination(certificate_data)

    # Auto-enable Life-of-Loan monitoring for every determination
    try:
        from db import set_life_of_loan, upsert_lol_monitoring, get_determination
        set_life_of_loan(record_id, True)
        det = get_determination(record_id)
        if det:
            upsert_lol_monitoring(det)
    except Exception as e:
        print(f"LOL auto-enable error (non-fatal): {e}")

    comm_info = nfip_community_info(
        certificate_data["community_number"],
        lat=certificate_data["lat"],
        lon=certificate_data["lon"],
    )

    return templates.TemplateResponse(request, "result.html", {
        "data": certificate_data,
        "record_id": record_id,
        "comm": comm_info,
        "current_user": session_user,
    })


@app.post("/download/certificate")
async def download_certificate(
    property_address: str = Form(...),
    matched_address: str = Form(...),
    loan_id: str = Form(...),
    borrower_name: str = Form(...),
    lender_name: str = Form(...),
    lender_address: str = Form(default=""),
    lender_email: str = Form(default=""),
    lat: str = Form(...),
    lon: str = Form(...),
    flood_zone: str = Form(...),
    flood_zone_description: str = Form(...),
    sfha_status: str = Form(...),
    insurance_required: str = Form(...),
    panel_number: str = Form(...),
    panel_effective_date: str = Form(...),
    community_number: str = Form(...),
    community_name: str = Form(...),
    county: str = Form(default=""),
    determination_date: str = Form(...),
    determination_date_iso: str = Form(...),
):
    data = dict(locals())
    try:
        data["map_image_b64"] = await generate_map_image(float(lat), float(lon))
    except Exception as e:
        print(f"Map image error (non-fatal): {e}")
        data["map_image_b64"] = None
    try:
        pdf_bytes = generate_flood_certificate_pdf(data)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")
    filename = _cert_filename(flood_zone, property_address)
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/download/notice")
async def download_notice(
    property_address: str = Form(...),
    matched_address: str = Form(...),
    loan_id: str = Form(...),
    borrower_name: str = Form(...),
    lender_name: str = Form(...),
    lender_address: str = Form(default=""),
    lender_email: str = Form(default=""),
    lat: str = Form(...),
    lon: str = Form(...),
    flood_zone: str = Form(...),
    flood_zone_description: str = Form(...),
    sfha_status: str = Form(...),
    insurance_required: str = Form(...),
    panel_number: str = Form(...),
    panel_effective_date: str = Form(...),
    community_number: str = Form(...),
    community_name: str = Form(...),
    county: str = Form(default=""),
    determination_date: str = Form(...),
    determination_date_iso: str = Form(...),
):
    data = dict(locals())
    data["map_image_b64"] = await generate_map_image(float(lat), float(lon))
    pdf_bytes = generate_borrower_notice_pdf(data)
    filename = f"borrower_notice_{loan_id}.pdf".replace(" ", "_")
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ── History routes ────────────────────────────────────────────────────────────

@app.get("/history", response_class=HTMLResponse)
async def history(request: Request, q: str = ""):
    user = get_session_user(request)
    company_id = None if (user and user.get("is_admin")) else (user.get("company_id") if user else None)
    if q.strip():
        records = search_determinations(q.strip(), company_id=company_id)
    else:
        records = list_determinations(100, company_id=company_id)
    return templates.TemplateResponse(request, "history.html", {
        "records": records,
        "query": q,
        "flagged_count": count_flagged(),
        "monitored_count": len(list_monitored()),
        "current_user": user,
    })


@app.get("/api/community/{community_number}")
async def community_status_api(community_number: str, lat: float = 0.0, lon: float = 0.0):
    return JSONResponse(nfip_community_info(community_number, lat=lat, lon=lon))


@app.post("/history/check-all")
async def check_all_monitored(request: Request):
    user = get_session_user(request)
    company_id = None if (user and user.get("is_admin")) else (user.get("company_id") if user else None)
    records = list_monitored()
    if company_id is not None:
        records = [r for r in records if r.get("company_id") == company_id]
    if not records:
        return JSONResponse({"checked": 0, "flagged": 0, "unchanged": 0, "errors": 0})
    checked, flagged, unchanged, errors = 0, 0, 0, 0
    sem = asyncio.Semaphore(3)

    async def _check_one(r):
        nonlocal checked, flagged, unchanged, errors
        lat, lon = r.get("lat"), r.get("lon")
        if not lat or not lon:
            errors += 1
            return
        try:
            zone_data, community_data, firm_data = await asyncio.gather(
                query_fema_nfhl(float(lat), float(lon)),
                query_nfip_community(float(lat), float(lon)),
                query_firm_panel(float(lat), float(lon)),
            )
            new_info = determine_flood_info({**zone_data, **community_data, **firm_data})
            changed = (
                r.get("flood_zone", "") != new_info.get("flood_zone", "") or
                r.get("panel_effective_date", "") != new_info.get("panel_effective_date", "")
            )
            flag_redetermination(r["id"], changed, date.today().strftime("%B %d, %Y"))
            checked += 1
            flagged += int(changed)
            unchanged += int(not changed)
            if changed and (r.get("lender_email") or "").strip():
                try:
                    send_redetermination_notification(r)
                except Exception as mail_exc:
                    print(f"Redetermination email error (record {r['id']}): {mail_exc}")
        except Exception:
            errors += 1

    async def bounded(r):
        async with sem:
            await _check_one(r)

    await asyncio.gather(*[bounded(r) for r in records])
    return JSONResponse({"checked": checked, "flagged": flagged, "unchanged": unchanged, "errors": errors})


@app.post("/history/{record_id}/monitor")
async def toggle_monitor(record_id: int, request: Request):
    record = get_determination(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    session_user = get_session_user(request)
    if not session_user:
        raise HTTPException(403)
    if not session_user.get("is_admin"):
        if record.get("company_id") and record.get("company_id") != session_user.get("company_id"):
            raise HTTPException(403)

    body = await request.json()
    enable = bool(body.get("enable", not record.get("life_of_loan", 0)))
    set_life_of_loan(record_id, enable)
    if enable:
        upsert_lol_monitoring(record)
    return JSONResponse({"record_id": record_id, "life_of_loan": int(enable)})


@app.post("/history/{record_id}/check")
async def check_fema_update(record_id: int):
    record = get_determination(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    lat, lon = record.get("lat"), record.get("lon")
    if not lat or not lon:
        return JSONResponse({"error": "No coordinates stored for this record"}, status_code=400)
    try:
        zone_data, community_data, firm_data = await asyncio.gather(
            query_fema_nfhl(float(lat), float(lon)),
            query_nfip_community(float(lat), float(lon)),
            query_firm_panel(float(lat), float(lon)),
        )
        new_info = determine_flood_info({**zone_data, **community_data, **firm_data})
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:300]}, status_code=502)

    old_zone  = record.get("flood_zone", "")
    old_panel = record.get("panel_effective_date", "")
    new_zone  = new_info.get("flood_zone", "")
    new_panel = new_info.get("panel_effective_date", "")
    changed = (old_zone != new_zone) or (old_panel != new_panel)
    checked_date = date.today().strftime("%B %d, %Y")
    flag_redetermination(record_id, changed, checked_date)
    email_sent = False
    if changed and (record.get("lender_email") or "").strip():
        try:
            send_redetermination_notification(record)
            email_sent = True
        except Exception as mail_exc:
            print(f"Redetermination email error (record {record_id}): {mail_exc}")
    return JSONResponse({
        "changed": changed,
        "old_zone": old_zone, "new_zone": new_zone,
        "old_panel": old_panel, "new_panel": new_panel,
        "needs_redetermination": changed,
        "checked_date": checked_date,
        "notification_sent": email_sent,
    })


@app.get("/history/{record_id}", response_class=HTMLResponse)
async def history_detail(request: Request, record_id: int):
    user = get_session_user(request)
    record = get_determination(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    if user and not user.get("is_admin"):
        if record.get("company_id") and record.get("company_id") != user.get("company_id"):
            raise HTTPException(403)
    comm_info = nfip_community_info(
        record.get("community_number", ""),
        lat=record.get("lat", 0.0),
        lon=record.get("lon", 0.0),
    )
    return templates.TemplateResponse(request, "result.html", {
        "data": record, "record_id": record_id,
        "from_history": True, "comm": comm_info, "current_user": user,
    })


@app.post("/history/{record_id}/download/certificate")
async def history_download_certificate(record_id: int):
    record = get_determination(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    pdf_bytes = generate_flood_certificate_pdf(record)
    filename = _cert_filename(record.get("flood_zone", ""), record.get("property_address", ""))
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/history/{record_id}/download/notice")
async def history_download_notice(record_id: int):
    record = get_determination(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    pdf_bytes = generate_borrower_notice_pdf(record)
    filename = f"borrower_notice_{record['loan_id']}.pdf".replace(" ", "_")
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/history/{record_id}/send-email")
async def history_send_email(record_id: int):
    record = get_determination(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    to_email = (record.get("lender_email") or "").strip()
    if not to_email:
        return RedirectResponse(
            url=f"/history/{record_id}?email_error=No+lender+email+address+on+file+for+this+record.",
            status_code=303,
        )
    try:
        pdf_bytes = generate_flood_certificate_pdf(record)
        send_certificate_email(to_email, record, pdf_bytes)
        return RedirectResponse(url=f"/history/{record_id}?email_sent=1&email_to={to_email}", status_code=303)
    except ValueError as exc:
        msg = str(exc).replace(" ", "+")
        return RedirectResponse(url=f"/history/{record_id}?email_error={msg}", status_code=303)
    except Exception as exc:
        msg = f"Failed+to+send+email:+{str(exc)[:120].replace(' ', '+')}".replace("&", "%26")
        return RedirectResponse(url=f"/history/{record_id}?email_error={msg}", status_code=303)


@app.get("/history/export/csv")
async def export_csv(request: Request, q: str = ""):
    user = get_session_user(request)
    company_id = None if (user and user.get("is_admin")) else (user.get("company_id") if user else None)
    if q.strip():
        records = search_determinations(q.strip(), company_id=company_id)
        filename = "flood_determinations_search.csv"
    else:
        records = list_determinations(limit=10000, company_id=company_id)
        filename = "flood_determinations_all.csv"

    columns = [
        "id", "determination_date", "loan_id", "borrower_name", "lender_name",
        "property_address", "matched_address", "lat", "lon",
        "flood_zone", "flood_zone_description", "sfha_status", "insurance_required",
        "panel_number", "panel_effective_date", "community_number", "community_name",
        "county", "created_at",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in records:
        writer.writerow(r)
    csv_bytes = output.getvalue().encode("utf-8-sig")
    return Response(content=csv_bytes, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ── LOL Monitoring routes ─────────────────────────────────────────────────────

@app.get("/lol-monitoring", response_class=HTMLResponse)
async def lol_monitoring_page(request: Request):
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    company_id = None if user.get("is_admin") else user.get("company_id")
    records = list_lol_monitoring(company_id=company_id)
    companies = list_companies() if user.get("is_admin") else []
    return templates.TemplateResponse(request, "lol_monitoring.html", {
        "records": records,
        "current_user": user,
        "companies": companies,
        "pending_count": count_pending_users() if user.get("is_admin") else 0,
    })


@app.post("/lol-monitoring/{monitoring_id}/close")
async def close_lol_record(request: Request, monitoring_id: int):
    user = get_session_user(request)
    if not user:
        raise HTTPException(403)
    rec = get_lol_monitoring(monitoring_id)
    if not rec:
        raise HTTPException(404)
    if not user.get("is_admin"):
        if rec.get("company_id") != user.get("company_id"):
            raise HTTPException(403, detail="Access denied")
    close_lol_monitoring(monitoring_id)
    return RedirectResponse("/lol-monitoring", status_code=303)


# ── Batch routes ──────────────────────────────────────────────────────────────

@app.get("/batch", response_class=HTMLResponse)
async def batch_page(request: Request):
    return templates.TemplateResponse(request, "batch.html", {"current_user": get_session_user(request)})


@app.get("/batch/template")
async def batch_template():
    header = "property_address,loan_id,borrower_name,lender_name,lender_email\n"
    rows = (
        "123 Main St, Houston TX 77002,2024-001,John Doe,First National Bank,jdoe.loan@firstnational.com\n"
        "456 Oak Ave, Miami FL 33101,2024-002,Jane Smith,Coastal Lenders LLC,jsmith@coastallenders.com\n"
        "789 River Rd, New Orleans LA 70112,2024-003,Bob Johnson,Gulf Coast Mortgage,\n"
    )
    csv_bytes = (header + rows).encode("utf-8-sig")
    return Response(content=csv_bytes, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="batch_template.csv"'})


async def _process_row(row: dict, det_date: str, det_date_iso: str, company_id=None, user_id=None) -> dict:
    property_address = row.get("property_address", "").strip()
    loan_id          = row.get("loan_id", "").strip()
    borrower_name    = row.get("borrower_name", "").strip()
    lender_name      = row.get("lender_name", "").strip()
    lender_email     = row.get("lender_email", "").strip()

    if not all([property_address, loan_id, borrower_name, lender_name]):
        return {
            "property_address": property_address, "loan_id": loan_id,
            "borrower_name": borrower_name, "lender_name": lender_name,
            "error": "Missing required fields",
        }

    parsed = parse_state_zip(property_address)
    if parsed:
        state, zipcode = parsed
        try:
            validate_us_address(state, zipcode)
        except HTTPException as exc:
            return {
                "property_address": property_address, "loan_id": loan_id,
                "borrower_name": borrower_name, "lender_name": lender_name,
                "error": exc.detail,
            }

    geo = await geocode_address(property_address)
    if not geo:
        return {
            "property_address": property_address, "loan_id": loan_id,
            "borrower_name": borrower_name, "lender_name": lender_name,
            "error": "Address could not be geocoded",
        }

    zone_data, community_data, firm_data, county_data, csb_data = await asyncio.gather(
        query_fema_nfhl(geo["lat"], geo["lon"]),
        query_nfip_community(geo["lat"], geo["lon"]),
        query_firm_panel(geo["lat"], geo["lon"], county_fips=geo.get("state_fips","") + geo.get("county_fips","")),
        query_county_name(geo["lat"], geo["lon"]),
        query_nfip_community_csb(
            geo.get("state_fips", ""),
            geo.get("county_fips", ""),
            geo.get("city", ""),
            geo.get("state_abbr", ""),
        ),
    )
    flood_info = determine_flood_info({
        **zone_data, **community_data, **firm_data, **county_data, **csb_data,
        "geocoded_city": geo.get("city", ""),
        "state_fips": geo.get("state_fips", ""),
        "county_fips": geo.get("county_fips", ""),
        "county_name": geo.get("county_name", ""),
    })

    data = {
        "property_address": property_address,
        "matched_address": geo.get("matched_address", property_address),
        "loan_id": loan_id, "borrower_name": borrower_name,
        "lender_name": lender_name, "lender_email": lender_email,
        "lat": geo["lat"], "lon": geo["lon"],
        "flood_zone": flood_info["flood_zone"],
        "flood_zone_description": flood_info["flood_zone_description"],
        "sfha_status": flood_info["sfha_status"],
        "insurance_required": flood_info["insurance_required"],
        "panel_number": flood_info["panel_number"],
        "panel_effective_date": flood_info["panel_effective_date"],
        "community_number": flood_info["community_number"],
        "community_name": flood_info["community_name"],
        "county": flood_info.get("county", ""),
        "determination_date": det_date,
        "determination_date_iso": det_date_iso,
        "company_id": company_id,
        "user_id": user_id,
    }
    record_id = save_determination(data)
    return {**data, "record_id": record_id}


@app.post("/batch", response_class=HTMLResponse)
async def batch_process(request: Request, csv_file: UploadFile = File(...)):
    errors = []
    if not csv_file.filename.lower().endswith(".csv"):
        errors.append("File must be a .csv file.")
        return templates.TemplateResponse(request, "batch.html", {"errors": errors})

    raw = await csv_file.read()
    try:
        text = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1").strip()
        except Exception:
            errors.append("Could not decode the CSV file. Please save it as UTF-8.")
            return templates.TemplateResponse(request, "batch.html", {"errors": errors})

    reader = csv.DictReader(io.StringIO(text))
    required_cols = {"property_address", "loan_id", "borrower_name", "lender_name"}
    if not reader.fieldnames or not required_cols.issubset({c.strip().lower() for c in reader.fieldnames}):
        errors.append(
            f"CSV must contain these columns: {', '.join(sorted(required_cols))}. "
            f"Found: {', '.join(reader.fieldnames or [])}."
        )
        return templates.TemplateResponse(request, "batch.html", {"errors": errors})

    rows = list(reader)
    if len(rows) == 0:
        errors.append("The CSV file contains no data rows.")
        return templates.TemplateResponse(request, "batch.html", {"errors": errors})
    if len(rows) > 100:
        errors.append(f"Maximum 100 rows per batch. Your file contains {len(rows)} rows.")
        return templates.TemplateResponse(request, "batch.html", {"errors": errors})

    normalised_rows = [{k.strip().lower(): v for k, v in r.items()} for r in rows]
    det_date = date.today().strftime("%B %d, %Y")
    det_date_iso = date.today().isoformat()

    session_user = get_session_user(request)
    s_company_id = session_user.get("company_id") if session_user else None
    s_user_id = session_user.get("id") if session_user else None
    if session_user and session_user.get("is_admin") and not s_company_id:
        s_company_id = ADMIN_COMPANY_ID

    sem = asyncio.Semaphore(5)

    async def bounded(row):
        async with sem:
            return await _process_row(row, det_date, det_date_iso, company_id=s_company_id, user_id=s_user_id)

    results = await asyncio.gather(*[bounded(r) for r in normalised_rows])

    out_columns = [
        "loan_id", "borrower_name", "lender_name",
        "property_address", "matched_address", "lat", "lon",
        "flood_zone", "flood_zone_description", "sfha_status", "insurance_required",
        "panel_number", "panel_effective_date", "community_number", "community_name",
        "county", "determination_date", "record_id", "error",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=out_columns, extrasaction="ignore")
    writer.writeheader()
    for r in results:
        writer.writerow(r)

    batch_id = str(uuid.uuid4())
    _batch_results[batch_id] = buf.getvalue().encode("utf-8-sig")
    _batch_record_ids[batch_id] = [r["record_id"] for r in results if r.get("record_id")]
    _batch_full_results[batch_id] = list(results)

    return templates.TemplateResponse(request, "batch_results.html", {
        "results": results,
        "batch_id": batch_id, "determination_date": det_date,
    })


@app.post("/batch/{batch_id}/email-all")
async def batch_email_all(batch_id: str):
    record_ids = _batch_record_ids.get(batch_id)
    if record_ids is None:
        return JSONResponse({"error": "Batch not found or expired."}, status_code=404)

    sent, skipped, failed, details = 0, 0, 0, []

    async def _send_one(rid: int):
        nonlocal sent, skipped, failed
        record = get_determination(rid)
        if not record:
            failed += 1
            details.append({"record_id": rid, "status": "error", "msg": "Record not found"})
            return
        to_email = (record.get("lender_email") or "").strip()
        if not to_email:
            skipped += 1
            details.append({"record_id": rid, "loan_id": record.get("loan_id"), "status": "skipped", "msg": "No lender email"})
            return
        try:
            pdf_bytes = generate_flood_certificate_pdf(record)
            send_certificate_email(to_email, record, pdf_bytes)
            sent += 1
            details.append({"record_id": rid, "loan_id": record.get("loan_id"), "status": "sent", "to": to_email})
        except Exception as exc:
            failed += 1
            details.append({"record_id": rid, "loan_id": record.get("loan_id"), "status": "error", "msg": str(exc)[:200]})

    sem = asyncio.Semaphore(3)

    async def bounded(rid):
        async with sem:
            await _send_one(rid)

    await asyncio.gather(*[bounded(rid) for rid in record_ids])
    return JSONResponse({"sent": sent, "skipped": skipped, "failed": failed, "details": details})


@app.get("/fema-test")
async def fema_test(request: Request):
    return templates.TemplateResponse("fema_test.html", {"request": request})


@app.get("/batch/download/{batch_id}")
async def batch_download(batch_id: str):
    csv_bytes = _batch_results.get(batch_id)
    if not csv_bytes:
        raise HTTPException(status_code=404, detail="Batch result not found or expired.")
    filename = f"flood_batch_{date.today().isoformat()}.csv"
    return Response(content=csv_bytes, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/batch/{batch_id}/report")
async def batch_report_pdf(batch_id: str):
    results = _batch_full_results.get(batch_id)
    if results is None:
        raise HTTPException(status_code=404, detail="Batch report not found or expired.")
    if not results:
        raise HTTPException(status_code=404, detail="No results in this batch.")
    det_date = next(
        (r.get("determination_date") for r in results if r.get("determination_date")),
        date.today().strftime("%B %d, %Y"),
    )
    pdf_bytes = generate_batch_report_pdf(results, det_date)
    filename = f"batch_flood_report_{date.today().isoformat()}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ── User Profile ──────────────────────────────────────────────────────────────

@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    from db import get_determinations
    all_records = get_determinations(user_id=user["id"])
    return templates.TemplateResponse(
        request, "profile.html",
        {
            "user": user,
            "record_count": len(all_records) if all_records else 0,
            "success": request.session.pop("profile_success", None),
            "error": request.session.pop("profile_error", None),
        }
    )


@app.post("/profile/change-password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    db_user = get_user_by_email(user["email"])
    if not db_user or not verify_password(current_password, db_user["password_hash"]):
        request.session["profile_error"] = "Current password is incorrect."
        return RedirectResponse("/profile", status_code=303)
    if new_password != confirm_password:
        request.session["profile_error"] = "New passwords do not match."
        return RedirectResponse("/profile", status_code=303)
    if len(new_password) < 8:
        request.session["profile_error"] = "Password must be at least 8 characters."
        return RedirectResponse("/profile", status_code=303)
    update_user_password(db_user["id"], hash_password(new_password))
    request.session["profile_success"] = "Password updated successfully."
    return RedirectResponse("/profile", status_code=303)


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_get(request: Request):
    return templates.TemplateResponse("forgot_password.html", {
        "request": request, "sent": False, "error": None,
    })


@app.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_post(request: Request, email: str = Form(...)):
    import secrets
    from datetime import timedelta
    db_user = get_user_by_email(email.strip().lower())
    if db_user and db_user.get("status") == "active":
        token  = secrets.token_urlsafe(32)
        expiry = (datetime.utcnow() + timedelta(hours=1)).isoformat()
        set_reset_token(db_user["id"], token, expiry)
        base = str(request.base_url).rstrip("/")
        reset_url = f"{base}/reset-password?token={token}"
        try:
            send_password_reset_email(db_user["email"], db_user.get("name", ""), reset_url)
        except Exception as e:
            print(f"[FORGOT-PW] email failed: {e}")
    return templates.TemplateResponse("forgot_password.html", {
        "request": request, "sent": True, "error": None,
    })


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_get(request: Request, token: str = ""):
    db_user = get_user_by_reset_token(token)
    expired = False
    if db_user:
        expiry = db_user.get("password_reset_expiry") or ""
        if expiry and datetime.utcnow().isoformat() > expiry:
            expired = True
            db_user = None
    return templates.TemplateResponse("reset_password.html", {
        "request": request, "token": token,
        "valid": db_user is not None, "expired": expired,
        "success": False, "error": None,
    })


@app.post("/reset-password", response_class=HTMLResponse)
async def reset_password_post(
    request: Request,
    token: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    db_user = get_user_by_reset_token(token)
    if not db_user:
        return templates.TemplateResponse("reset_password.html", {
            "request": request, "token": token,
            "valid": False, "expired": False,
            "success": False, "error": "Invalid or expired link.",
        })
    expiry = db_user.get("password_reset_expiry") or ""
    if expiry and datetime.utcnow().isoformat() > expiry:
        return templates.TemplateResponse("reset_password.html", {
            "request": request, "token": token,
            "valid": False, "expired": True,
            "success": False, "error": "Link has expired. Please request a new one.",
        })
    if new_password != confirm_password:
        return templates.TemplateResponse("reset_password.html", {
            "request": request, "token": token,
            "valid": True, "expired": False,
            "success": False, "error": "Passwords do not match.",
        })
    if len(new_password) < 8:
        return templates.TemplateResponse("reset_password.html", {
            "request": request, "token": token,
            "valid": True, "expired": False,
            "success": False, "error": "Password must be at least 8 characters.",
        })
    update_user_password(db_user["id"], hash_password(new_password))
    clear_reset_token(db_user["id"])
    return templates.TemplateResponse("reset_password.html", {
        "request": request, "token": "",
        "valid": True, "expired": False,
        "success": True, "error": None,
    })


async def refresh_nfhl_weekly():
    """Download and reimport NFHL data weekly from FEMA MSC."""
    print("[NFHL] Weekly refresh would run here")
