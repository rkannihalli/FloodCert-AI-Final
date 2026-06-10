import os
import csv
import io
import asyncio
import uuid
from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, Response, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import date
from pdf_generator import generate_flood_certificate_pdf, generate_borrower_notice_pdf
from fema_lookup import geocode_address, query_fema_nfhl, determine_flood_info
from db import init_db, save_determination, get_determination, search_determinations, list_determinations, delete_determination
from email_sender import send_certificate_email

# In-memory store for batch CSV results (keyed by batch_id)
_batch_results: dict[str, bytes] = {}
# In-memory store for batch record IDs (for bulk email)
_batch_record_ids: dict[str, list[int]] = {}

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


@app.post("/generate", response_class=HTMLResponse)
async def generate(
    request: Request,
    property_address: str = Form(...),
    loan_id: str = Form(...),
    borrower_name: str = Form(...),
    lender_name: str = Form(...),
    lender_email: str = Form(default=""),
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

    fema_data = await query_fema_nfhl(geo_result["lat"], geo_result["lon"])
    flood_info = determine_flood_info(fema_data)

    certificate_data = {
        "property_address": property_address,
        "matched_address": geo_result.get("matched_address", property_address),
        "loan_id": loan_id,
        "borrower_name": borrower_name,
        "lender_name": lender_name,
        "lender_email": lender_email.strip(),
        "lat": geo_result["lat"],
        "lon": geo_result["lon"],
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

    return templates.TemplateResponse("result.html", {
        "request": request,
        "data": certificate_data,
        "record_id": record_id,
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
    })


@app.get("/history/{record_id}", response_class=HTMLResponse)
async def history_detail(request: Request, record_id: int):
    record = get_determination(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return templates.TemplateResponse("result.html", {
        "request": request,
        "data": record,
        "record_id": record_id,
        "from_history": True,
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

    geo = await geocode_address(property_address)
    if not geo:
        return {
            "property_address": property_address,
            "loan_id": loan_id,
            "borrower_name": borrower_name,
            "lender_name": lender_name,
            "error": "Address could not be geocoded",
        }

    fema_data = await query_fema_nfhl(geo["lat"], geo["lon"])
    flood_info = determine_flood_info(fema_data)

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
