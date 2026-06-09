import os
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML, CSS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

jinja_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))


def _render_pdf(template_name: str, data: dict) -> bytes:
    template = jinja_env.get_template(template_name)
    html_content = template.render(**data)
    css_path = os.path.join(STATIC_DIR, "css", "pdf.css")
    pdf = HTML(string=html_content, base_url=BASE_DIR).write_pdf(
        stylesheets=[CSS(filename=css_path)]
    )
    return pdf


def generate_flood_certificate_pdf(data: dict) -> bytes:
    return _render_pdf("certificate_pdf.html", data)


def generate_borrower_notice_pdf(data: dict) -> bytes:
    return _render_pdf("notice_pdf.html", data)
