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
    
    # weasyprint 57.x API: HTML(string=..., base_url=...)
    html_doc = weasyprint.HTML(string=html_str, base_url=base_url)
    
    if os.path.exists(css_path):
        # weasyprint 57.x: CSS(filename=...) 
        sheets = [weasyprint.CSS(filename=css_path)]
    else:
        sheets = []
    
    return html_doc.write_pdf(stylesheets=sheets)

def generate_flood_certificate_pdf(data: dict) -> bytes:
    return _render_pdf("certificate_pdf.html", data)

def generate_borrower_notice_pdf(data: dict) -> bytes:
    return _render_pdf("notice_pdf.html", data)

def generate_batch_report_pdf(results: list, batch_date: str) -> bytes:
    return _render_pdf("batch_report_pdf.html", {"results": results, "batch_date": batch_date})
