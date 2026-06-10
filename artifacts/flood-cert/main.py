import os
import csv
import io
import asyncio
import uuid
import re
from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, Response, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import date
from pdf_generator import generate_flood_certificate_pdf, generate_borrower_notice_pdf, generate_batch_report_pdf
from fema_lookup import (
    geocode_address, query_fema_nfhl, query_nfip_community, query_firm_panel,
    determine_flood_info, nfip_community_info,
)
from db import (
    init_db, save_determination, get_determination,
    search_determinations, list_determinations, delete_determination,
    set_life_of_loan, flag_redetermination, list_monitored, count_flagged,
)
from email_sender import send_certificate_email

US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA",
    "HI","ID","IL","IN","IA","KS","KY","LA","ME","MD",
    "MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC",
    "SD","TN","TX","UT","VT","VA","WA","WV","WI","WY",
    "DC","PR","GU","VI",
}

def parse_state_zip(address: str) -> tuple[str, str] | None:
    """Extract (state_abbr, zipcode) from a free-text US address string."""
    m = re.search(r'\b([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\s*$', address.strip())
    return (m.group(1).upper(), m.group(2)) if m else None

def validate_us_address(state: str, zipcode: str) -> None:
    if state.upper() not in US_STATES:
        raise HTTPException(400, detail="Only US addresses supported")
    if not re.match(r"^\d{5}(-\d{4})?$", zipcode):
        raise HTTPException(400, detail="Invalid US ZIP code format")

# In-memory store for batch CSV results (keyed by batch_id)
_batch_results: dict[str, bytes] = {}
# In-memory store for batch record IDs (for bulk email)
_batch_record_ids: dict[str, list[int]] = {}
# In-memory store for full batch results (for report PDF)
_batch_full_results: dict[str, list] = {}

app = FastAPI(title="FEMA Flood Certificate Generator")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.on_event("startup")
async def startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/geocode")
async def geocode_endpoint(request: Request):
    from fastapi.responses import JSONResponse
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

    if errors:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "errors": errors,
            "form": {
                "property_address": property_address,
                "loan_id": loan_id,
                "borrower_name": borrower_name,
                "lender_name": lender_name,
                "lender_email": lender_email,
            }
        })

    precomputed = lat.strip() and lon.strip() and flood_zone.strip()

    if precomputed:
        flood_info = {
            "flood_zone": flood_zone,
            "flood_zone_description": flood_zone_description,
            "sfha_status": sfha_status,
            "insurance_required": insurance_required,
            "panel_number": panel_number,
            "panel_effective_date": panel_effective_date,
            "community_number": community_number,
            "community_name": community_name,
        }
        geo_lat = float(lat)
        geo_lon = float(lon)
        geo_matched = matched_address or property_address
    else:
        geo_result = await geocode_address(property_address)
        if not geo_result:
            return templates.TemplateResponse("index.html", {
                "request": request,
                "errors": ["Could not geocode the provided address. Please check the address and try again."],
                "form": {
                    "property_address": property_address,
                    "loan_id": loan_id,
                    "borrower_name": borrower_name,
                    "lender_name": lender_name,
                    "lender_email": lender_email,
                }
            })
        zone_data, community_data, firm_data = await asyncio.gather(
            query_fema_nfhl(geo_result["lat"], geo_result["lon"]),
            query_nfip_community(geo_result["lat"], geo_result["lon"]),
            query_firm_panel(geo_result["lat"], geo_result["lon"]),
        )
        flood_info = determine_flood_info({
            **zone_data, **community_data, **firm_data,
            "geocoded_city": geo_result.get("city", ""),
        })
        geo_lat = geo_result["lat"]
        geo_lon = geo_result["lon"]
        geo_matched = geo_result.get("matched_address", property_address)

    certificate_data = {
        "property_address": property_address,
        "matched_address": geo_matched,
        "loan_id": loan_id,
        "borrower_name": borrower_name,
        "lender_name": lender_name,
        "lender_email": lender_email.strip(),
        "lat": geo_lat,
        "lon": geo_lon,
        "flood_zone": flood_info["flood_zone"],
        "flood_zone_description": flood_info["flood_zone_description"],
        "sfha_status": flood_info["sfha_status"],
        "insurance_required": flood_info["insurance_required"],
        "panel_number": flood_info["panel_number"],
        "panel_effective_date": flood_info["panel_effective_date"],
        "community_number": flood_info["community_number"],
        "community_name": flood_info["community_name"],
        "determination_date": date.today().strftime("%B %d, %Y"),
        "determination_date_iso": date.today().isoformat(),
    }

    record_id = save_determination(certificate_data)

    comm_info = nfip_community_info(
        certificate_data["community_number"],
        lat=certificate_data["lat"],
        lon=certificate_data["lon"],
    )

    return templates.TemplateResponse("result.html", {
        "request": request,
        "data": certificate_data,
        "record_id": record_id,
        "comm": comm_info,
    })


def _build_data_from_form(**kwargs) -> dict:
    return {k: v for k, v in kwargs.items()}


@app.post("/download/certificate")
async def download_certificate(
    property_address: str = Form(...),
    matched_address: str = Form(...),
    loan_id: str = Form(...),
    borrower_name: str = Form(...),
    lender_name: str = Form(...),
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
    determination_date: str = Form(...),
    determination_date_iso: str = Form(...),
):
    data = dict(locals())
    pdf_bytes = generate_flood_certificate_pdf(data)
    filename = f"flood_certificate_{loan_id}.pdf".replace(" ", "_")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/download/notice")
async def download_notice(
    property_address: str = Form(...),
    matched_address: str = Form(...),
    loan_id: str = Form(...),
    borrower_name: str = Form(...),
    lender_name: str = Form(...),
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
    determination_date: str = Form(...),
    determination_date_iso: str = Form(...),
):
    data = dict(locals())
    pdf_bytes = generate_borrower_notice_pdf(data)
    filename = f"borrower_notice_{loan_id}.pdf".replace(" ", "_")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request, q: str = ""):
    if q.strip():
        records = search_determinations(q.strip())
    else:
        records = list_determinations(50)
    return templates.TemplateResponse("history.html", {
        "request": request,
        "records": records,
        "query": q,
        "flagged_count": count_flagged(),
        "monitored_count": len(list_monitored()),
    })


@app.get("/api/community/{community_number}")
async def community_status_api(
    community_number: str,
    lat: float = 0.0,
    lon: float = 0.0,
):
    from fastapi.responses import JSONResponse
    return JSONResponse(nfip_community_info(community_number, lat=lat, lon=lon))


@app.post("/history/check-all")
async def check_all_monitored():
    from fastapi.responses import JSONResponse
    records = list_monitored()
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
        except Exception:
            errors += 1

    async def bounded(r):
        async with sem:
            await _check_one(r)

    await asyncio.gather(*[bounded(r) for r in records])
    return JSONResponse({"checked": checked, "flagged": flagged, "unchanged": unchanged, "errors": errors})


@app.post("/history/{record_id}/monitor")
async def toggle_monitor(record_id: int, request: Request):
    from fastapi.responses import JSONResponse
    record = get_determination(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    body = await request.json()
    enable = bool(body.get("enable", not record.get("life_of_loan", 0)))
    set_life_of_loan(record_id, enable)
    return JSONResponse({"record_id": record_id, "life_of_loan": int(enable)})


@app.post("/history/{record_id}/check")
async def check_fema_update(record_id: int):
    from fastapi.responses import JSONResponse
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
    return JSONResponse({
        "changed": changed,
        "old_zone": old_zone, "new_zone": new_zone,
        "old_panel": old_panel, "new_panel": new_panel,
        "needs_redetermination": changed,
        "checked_date": checked_date,
    })


@app.get("/history/{record_id}", response_class=HTMLResponse)
async def history_detail(request: Request, record_id: int):
    record = get_determination(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    comm_info = nfip_community_info(
        record.get("community_number", ""),
        lat=record.get("lat", 0.0),
        lon=record.get("lon", 0.0),
    )
    return templates.TemplateResponse("result.html", {
        "request": request,
        "data": record,
        "record_id": record_id,
        "from_history": True,
        "comm": comm_info,
    })


@app.post("/history/{record_id}/download/certificate")
async def history_download_certificate(record_id: int):
    record = get_determination(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    pdf_bytes = generate_flood_certificate_pdf(record)
    filename = f"flood_certificate_{record['loan_id']}.pdf".replace(" ", "_")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/history/{record_id}/download/notice")
async def history_download_notice(record_id: int):
    record = get_determination(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    pdf_bytes = generate_borrower_notice_pdf(record)
    filename = f"borrower_notice_{record['loan_id']}.pdf".replace(" ", "_")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/history/{record_id}/delete")
async def history_delete(record_id: int):
    delete_determination(record_id)
    return RedirectResponse(url="/history", status_code=303)


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
        return RedirectResponse(
            url=f"/history/{record_id}?email_sent=1&email_to={to_email}",
            status_code=303,
        )
    except ValueError as exc:
        msg = str(exc).replace(" ", "+")
        return RedirectResponse(url=f"/history/{record_id}?email_error={msg}", status_code=303)
    except Exception as exc:
        msg = f"Failed+to+send+email:+{str(exc)[:120].replace(' ', '+')}".replace("&", "%26")
        return RedirectResponse(url=f"/history/{record_id}?email_error={msg}", status_code=303)


@app.get("/batch", response_class=HTMLResponse)
async def batch_page(request: Request):
    return templates.TemplateResponse("batch.html", {"request": request})


@app.get("/batch/template")
async def batch_template():
    header = "property_address,loan_id,borrower_name,lender_name,lender_email\n"
    rows = (
        "123 Main St, Houston TX 77002,2024-001,John Doe,First National Bank,jdoe.loan@firstnational.com\n"
        "456 Oak Ave, Miami FL 33101,2024-002,Jane Smith,Coastal Lenders LLC,jsmith@coastallenders.com\n"
        "789 River Rd, New Orleans LA 70112,2024-003,Bob Johnson,Gulf Coast Mortgage,\n"
    )
    csv_bytes = (header + rows).encode("utf-8-sig")
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="batch_template.csv"'},
    )


async def _process_row(row: dict, det_date: str, det_date_iso: str) -> dict:
    property_address = row.get("property_address", "").strip()
    loan_id = row.get("loan_id", "").strip()
    borrower_name = row.get("borrower_name", "").strip()
    lender_name = row.get("lender_name", "").strip()
    lender_email = row.get("lender_email", "").strip()

    if not all([property_address, loan_id, borrower_name, lender_name]):
        return {
            "property_address": property_address,
            "loan_id": loan_id,
            "borrower_name": borrower_name,
            "lender_name": lender_name,
            "error": "Missing required fields",
        }

    parsed = parse_state_zip(property_address)
    if parsed:
        state, zipcode = parsed
        try:
            validate_us_address(state, zipcode)
        except HTTPException as exc:
            return {
                "property_address": property_address,
                "loan_id": loan_id,
                "borrower_name": borrower_name,
                "lender_name": lender_name,
                "error": exc.detail,
            }

    geo = await geocode_address(property_address)
    if not geo:
        return {
            "property_address": property_address,
            "loan_id": loan_id,
            "borrower_name": borrower_name,
            "lender_name": lender_name,
            "error": "Address could not be geocoded",
        }

    zone_data, community_data, firm_data = await asyncio.gather(
        query_fema_nfhl(geo["lat"], geo["lon"]),
        query_nfip_community(geo["lat"], geo["lon"]),
        query_firm_panel(geo["lat"], geo["lon"]),
    )
    flood_info = determine_flood_info({
        **zone_data, **community_data, **firm_data,
        "geocoded_city": geo.get("city", ""),
    })

    data = {
        "property_address": property_address,
        "matched_address": geo.get("matched_address", property_address),
        "loan_id": loan_id,
        "borrower_name": borrower_name,
        "lender_name": lender_name,
        "lender_email": lender_email,
        "lat": geo["lat"],
        "lon": geo["lon"],
        "flood_zone": flood_info["flood_zone"],
        "flood_zone_description": flood_info["flood_zone_description"],
        "sfha_status": flood_info["sfha_status"],
        "insurance_required": flood_info["insurance_required"],
        "panel_number": flood_info["panel_number"],
        "panel_effective_date": flood_info["panel_effective_date"],
        "community_number": flood_info["community_number"],
        "community_name": flood_info["community_name"],
        "determination_date": det_date,
        "determination_date_iso": det_date_iso,
    }
    record_id = save_determination(data)
    return {**data, "record_id": record_id}


@app.post("/batch", response_class=HTMLResponse)
async def batch_process(request: Request, csv_file: UploadFile = File(...)):
    errors = []

    if not csv_file.filename.lower().endswith(".csv"):
        errors.append("File must be a .csv file.")
        return templates.TemplateResponse("batch.html", {"request": request, "errors": errors})

    raw = await csv_file.read()
    try:
        text = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1").strip()
        except Exception:
            errors.append("Could not decode the CSV file. Please save it as UTF-8.")
            return templates.TemplateResponse("batch.html", {"request": request, "errors": errors})

    reader = csv.DictReader(io.StringIO(text))
    required_cols = {"property_address", "loan_id", "borrower_name", "lender_name"}
    if not reader.fieldnames or not required_cols.issubset({c.strip().lower() for c in reader.fieldnames}):
        errors.append(
            f"CSV must contain these columns: {', '.join(sorted(required_cols))}. "
            f"Found: {', '.join(reader.fieldnames or [])}."
        )
        return templates.TemplateResponse("batch.html", {"request": request, "errors": errors})

    rows = list(reader)
    if len(rows) == 0:
        errors.append("The CSV file contains no data rows.")
        return templates.TemplateResponse("batch.html", {"request": request, "errors": errors})
    if len(rows) > 100:
        errors.append(f"Maximum 100 rows per batch. Your file contains {len(rows)} rows.")
        return templates.TemplateResponse("batch.html", {"request": request, "errors": errors})

    # Normalise column names to lowercase stripped
    normalised_rows = [{k.strip().lower(): v for k, v in r.items()} for r in rows]

    det_date = date.today().strftime("%B %d, %Y")
    det_date_iso = date.today().isoformat()

    # Process concurrently with a semaphore to avoid hammering the APIs
    sem = asyncio.Semaphore(5)

    async def bounded(row):
        async with sem:
            return await _process_row(row, det_date, det_date_iso)

    results = await asyncio.gather(*[bounded(r) for r in normalised_rows])

    # Build CSV output
    out_columns = [
        "loan_id", "borrower_name", "lender_name",
        "property_address", "matched_address", "lat", "lon",
        "flood_zone", "flood_zone_description", "sfha_status", "insurance_required",
        "panel_number", "panel_effective_date", "community_number", "community_name",
        "determination_date", "record_id", "error",
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

    return templates.TemplateResponse("batch_results.html", {
        "request": request,
        "results": results,
        "batch_id": batch_id,
        "determination_date": det_date,
    })


@app.post("/batch/{batch_id}/email-all")
async def batch_email_all(batch_id: str):
    from fastapi.responses import JSONResponse
    record_ids = _batch_record_ids.get(batch_id)
    if record_ids is None:
        return JSONResponse({"error": "Batch not found or expired. Re-run the batch to email certificates."}, status_code=404)

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

    # Run with a semaphore so we don't flood the SMTP server
    sem = asyncio.Semaphore(3)
    async def bounded(rid):
        async with sem:
            await _send_one(rid)

    await asyncio.gather(*[bounded(rid) for rid in record_ids])
    return JSONResponse({"sent": sent, "skipped": skipped, "failed": failed, "details": details})


@app.get("/batch/download/{batch_id}")
async def batch_download(batch_id: str):
    csv_bytes = _batch_results.get(batch_id)
    if not csv_bytes:
        raise HTTPException(status_code=404, detail="Batch result not found or expired. Please re-run the batch.")
    filename = f"flood_batch_{date.today().isoformat()}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/batch/{batch_id}/report")
async def batch_report_pdf(batch_id: str):
    results = _batch_full_results.get(batch_id)
    if results is None:
        raise HTTPException(status_code=404, detail="Batch report not found or expired. Please re-run the batch.")
    if not results:
        raise HTTPException(status_code=404, detail="No results in this batch.")
    det_date = next(
        (r.get("determination_date") for r in results if r.get("determination_date")),
        date.today().strftime("%B %d, %Y"),
    )
    pdf_bytes = generate_batch_report_pdf(results, det_date)
    filename = f"batch_flood_report_{date.today().isoformat()}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/history/export/csv")
async def export_csv(q: str = ""):
    if q.strip():
        records = search_determinations(q.strip())
        filename = f"flood_determinations_search.csv"
    else:
        records = list_determinations(limit=10000)
        filename = f"flood_determinations_all.csv"

    columns = [
        "id", "determination_date", "loan_id", "borrower_name", "lender_name",
        "property_address", "matched_address", "lat", "lon",
        "flood_zone", "flood_zone_description", "sfha_status", "insurance_required",
        "panel_number", "panel_effective_date", "community_number", "community_name",
        "created_at",
    ]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in records:
        writer.writerow(r)

    csv_bytes = output.getvalue().encode("utf-8-sig")

    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
