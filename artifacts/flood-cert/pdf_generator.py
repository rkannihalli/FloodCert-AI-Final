# weasyprint 57.x compatible
import os
from jinja2 import Environment, FileSystemLoader

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR   = os.path.join(BASE_DIR, "static")
jinja_env    = Environment(loader=FileSystemLoader(TEMPLATE_DIR))

def _zone_label(code: str) -> str:
    from fema_lookup import ZONE_DISPLAY_NAMES
    return ZONE_DISPLAY_NAMES.get((code or "").strip().upper(), code or "")

jinja_env.filters["zone_label"] = _zone_label

def _render_pdf(template_name: str, data: dict) -> bytes:
    import weasyprint
    html_str = jinja_env.get_template(template_name).render(**data)
    css_path = os.path.join(STATIC_DIR, "css", "pdf.css")
    base_url = f"file://{STATIC_DIR}/"
    html_doc = weasyprint.HTML(string=html_str, base_url=base_url)
    sheets   = [weasyprint.CSS(filename=css_path)] if os.path.exists(css_path) else []
    return html_doc.write_pdf(stylesheets=sheets)

def generate_flood_certificate_pdf(data: dict) -> bytes:
    return _render_pdf("certificate_pdf.html", data)

def generate_borrower_notice_pdf(data: dict) -> bytes:
    return _render_pdf("notice_pdf.html", data)

def generate_batch_report_pdf(results: list, batch_date: str) -> bytes:
    sfha         = sum(1 for r in results if r.get("sfha_status") == "Yes")
    non_sfha     = sum(1 for r in results if r.get("sfha_status") == "No")
    undetermined = sum(1 for r in results if not r.get("error") and r.get("flood_zone") == "UNDETERMINED")
    errors       = sum(1 for r in results if r.get("error"))
    return _render_pdf("batch_report_pdf.html", {
        "results": results, "batch_date": batch_date,
        "total": len(results), "sfha_count": sfha,
        "non_sfha_count": non_sfha, "undetermined_count": undetermined,
        "error_count": errors,
    })
