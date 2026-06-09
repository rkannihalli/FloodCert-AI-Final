import os
import json
import httpx
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import date
from pdf_generator import generate_flood_certificate_pdf, generate_borrower_notice_pdf
from fema_lookup import geocode_address, query_fema_nfhl, determine_flood_info

app = FastAPI(title="FEMA Flood Certificate Generator")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


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

    return templates.TemplateResponse("result.html", {
        "request": request,
        "data": certificate_data,
    })


@app.post("/download/certificate")
async def download_certificate(
    property_address: str = Form(...),
    matched_address: str = Form(...),
    loan_id: str = Form(...),
    borrower_name: str = Form(...),
    lender_name: str = Form(...),
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
    data = {
        "property_address": property_address,
        "matched_address": matched_address,
        "loan_id": loan_id,
        "borrower_name": borrower_name,
        "lender_name": lender_name,
        "lat": lat,
        "lon": lon,
        "flood_zone": flood_zone,
        "flood_zone_description": flood_zone_description,
        "sfha_status": sfha_status,
        "insurance_required": insurance_required,
        "panel_number": panel_number,
        "panel_effective_date": panel_effective_date,
        "community_number": community_number,
        "community_name": community_name,
        "determination_date": determination_date,
        "determination_date_iso": determination_date_iso,
    }
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
    data = {
        "property_address": property_address,
        "matched_address": matched_address,
        "loan_id": loan_id,
        "borrower_name": borrower_name,
        "lender_name": lender_name,
        "lat": lat,
        "lon": lon,
        "flood_zone": flood_zone,
        "flood_zone_description": flood_zone_description,
        "sfha_status": sfha_status,
        "insurance_required": insurance_required,
        "panel_number": panel_number,
        "panel_effective_date": panel_effective_date,
        "community_number": community_number,
        "community_name": community_name,
        "determination_date": determination_date,
        "determination_date_iso": determination_date_iso,
    }
    pdf_bytes = generate_borrower_notice_pdf(data)
    filename = f"borrower_notice_{loan_id}.pdf".replace(" ", "_")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
