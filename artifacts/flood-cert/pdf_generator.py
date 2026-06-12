import os, io
from jinja2 import Environment, FileSystemLoader

PDF_ENGINE = "none"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
jinja_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))

def _zone_label(code: str) -> str:
    from fema_lookup import ZONE_DISPLAY_NAMES
    return ZONE_DISPLAY_NAMES.get((code or "").strip().upper(), code or "")

jinja_env.filters["zone_label"] = _zone_label

def _render_pdf(template_name: str, data: dict) -> bytes:
    template = jinja_env.get_template(template_name)
    html_content = template.render(**data)
    css_path = os.path.join(STATIC_DIR, "css", "pdf.css")
    return html_content.encode("utf-8")

def generate_flood_certificate_pdf(data: dict) -> bytes:
    return _render_pdf("certificate_pdf.html", data)

def generate_borrower_notice_pdf(data: dict) -> bytes:
    return _render_pdf("notice_pdf.html", data)

def generate_batch_report_pdf(results: list, batch_date: str) -> bytes:
    sfha_count = sum(1 for r in results if r.get("sfha_status") == "Yes")
    non_sfha_count = sum(1 for r in results if r.get("sfha_status") == "No")
    undetermined_count = sum(1 for r in results if not r.get("error") and r.get("flood_zone") == "UNDETERMINED")
    error_count = sum(1 for r in results if r.get("error"))
    return _render_pdf("batch_report_pdf.html", {"results": results, "batch_date": batch_date, "total": len(results), "sfha_count": sfha_count, "non_sfha_count": non_sfha_count, "undetermined_count": undetermined_count, "error_count": error_count})