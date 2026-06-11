import os
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def send_certificate_email(to_email: str, record: dict, pdf_bytes: bytes) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    from_addr = os.environ.get("FROM_EMAIL") or user

    if not user or not password:
        raise ValueError(
            "SMTP credentials not configured. "
            "Set SMTP_USER and SMTP_PASSWORD in your environment secrets."
        )

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_email
    msg["Subject"] = (
        f"Flood Hazard Determination — Loan {record['loan_id']} "
        f"({record.get('matched_address') or record.get('property_address', '')})"
    )

    sfha_line = (
        "⚠ This property IS in a Special Flood Hazard Area (SFHA). "
        "Federal flood insurance is required."
        if record.get("sfha_status") == "Yes"
        else "This property is NOT in a Special Flood Hazard Area."
    )

    body = f"""\
Dear {record['lender_name']},

Please find the attached Flood Hazard Determination Certificate for the following loan file.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Loan ID:           {record['loan_id']}
  Borrower:          {record['borrower_name']}
  Property:          {record.get('matched_address') or record.get('property_address', '')}
  Flood Zone:        {record['flood_zone']}
  SFHA Status:       {record['sfha_status']}
  Insurance:         {record['insurance_required']}
  Determination Date:{record['determination_date']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{sfha_line}

The attached PDF is the official flood determination certificate. A separate borrower notice
can be generated from the History page if required.

Data sources: US Census Bureau Geocoder + FEMA National Flood Hazard Layer (NFHL).

—
FEMA Flood Certificate Generator
"""

    msg.attach(MIMEText(body, "plain"))

    part = MIMEBase("application", "pdf")
    part.set_payload(pdf_bytes)
    encoders.encode_base64(part)
    safe_loan = record["loan_id"].replace(" ", "_").replace("/", "-")
    part.add_header(
        "Content-Disposition",
        "attachment",
        filename=f"flood_certificate_{safe_loan}.pdf",
    )
    msg.attach(part)

    with smtplib.SMTP(host, port, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.login(user, password)
        server.send_message(msg)


def send_redetermination_notification(record: dict) -> None:
    """Notify the lender that a FIRM panel change was detected and a re-run is needed."""
    to_email = (record.get("lender_email") or "").strip()
    if not to_email:
        raise ValueError("No lender email on record — cannot send redetermination notification.")

    host     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port     = int(os.environ.get("SMTP_PORT", "587"))
    user     = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    from_addr = os.environ.get("FROM_EMAIL") or user

    if not user or not password:
        raise ValueError(
            "SMTP credentials not configured. "
            "Set SMTP_USER and SMTP_PASSWORD in environment secrets."
        )

    prop_addr = record.get("matched_address") or record.get("property_address", "")

    msg = MIMEMultipart()
    msg["From"]    = from_addr
    msg["To"]      = to_email
    msg["Subject"] = (
        f"⚠ FIRM Map Change Detected — Re-Determination Required | "
        f"Loan {record['loan_id']} | {prop_addr}"
    )

    body = f"""\
Dear {record['lender_name']},

A change to the FEMA Flood Insurance Rate Map (FIRM) panel covering the property below
has been detected during automated Life-of-Loan monitoring. Under SFHDF and regulatory
guidance, a re-determination is required when FIRM map revisions affect a property.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Loan ID:             {record['loan_id']}
  Borrower:            {record['borrower_name']}
  Property:            {prop_addr}
  Original Flood Zone: {record.get('flood_zone', 'N/A')}
  Original Map Panel:  {record.get('community_number', 'N/A')}
  Original Panel Date: {record.get('panel_effective_date', 'N/A')}
  Original Determination: {record.get('determination_date', 'N/A')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ACTION REQUIRED
Please log in to the FEMA Flood Certificate Generator and re-run a new flood
determination for this property to obtain current FIRM panel data and an updated
Standard Flood Hazard Determination Form (SFHDF).

A map revision may affect the property's Special Flood Hazard Area (SFHA) status,
flood insurance requirements, and applicable flood zone designation.

—
FEMA Flood Certificate Generator | Life-of-Loan Monitoring Service
"""

    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(host, port, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
