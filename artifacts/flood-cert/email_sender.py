import os
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _smtp_config() -> tuple[str, int, str, str, str]:
    host     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port     = int(os.environ.get("SMTP_PORT", "587"))
    user     = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    from_addr = os.environ.get("FROM_EMAIL") or user
    return host, port, user, password, from_addr


def _send(msg: MIMEMultipart) -> None:
    host, port, user, password, _ = _smtp_config()
    if not user or not password:
        raise ValueError("SMTP credentials not configured. Set SMTP_USER and SMTP_PASSWORD.")
    with smtplib.SMTP(host, port, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.login(user, password)
        server.send_message(msg)


def send_certificate_email(to_email: str, record: dict, pdf_bytes: bytes) -> None:
    _, _, _, _, from_addr = _smtp_config()
    msg = MIMEMultipart()
    msg["From"]    = from_addr
    msg["To"]      = to_email
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

The attached PDF is the official flood determination certificate.

Data sources: US Census Bureau Geocoder + FEMA National Flood Hazard Layer (NFHL).

—
FEMA Flood Certificate Generator
"""
    msg.attach(MIMEText(body, "plain"))
    part = MIMEBase("application", "pdf")
    part.set_payload(pdf_bytes)
    encoders.encode_base64(part)
    safe_loan = record["loan_id"].replace(" ", "_").replace("/", "-")
    part.add_header("Content-Disposition", "attachment", filename=f"flood_certificate_{safe_loan}.pdf")
    msg.attach(part)
    _send(msg)


def send_redetermination_notification(record: dict) -> None:
    """Notify the lender that a FIRM panel change was detected."""
    to_email = (record.get("lender_email") or "").strip()
    if not to_email:
        raise ValueError("No lender email on record.")
    _, _, _, _, from_addr = _smtp_config()
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
has been detected during automated Life-of-Loan monitoring.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Loan ID:             {record['loan_id']}
  Borrower:            {record['borrower_name']}
  Property:            {prop_addr}
  Original Flood Zone: {record.get('flood_zone', 'N/A')}
  Original Map Panel:  {record.get('community_number', 'N/A')}
  Original Panel Date: {record.get('panel_effective_date', 'N/A')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ACTION REQUIRED: Please re-run the flood determination for this property.

—
FEMA Flood Certificate Generator | Life-of-Loan Monitoring
"""
    msg.attach(MIMEText(body, "plain"))
    _send(msg)


# ── New email functions ────────────────────────────────────────────────────────

def send_access_request_confirmation(to_email: str, user_data: dict) -> None:
    """Confirmation to the user after they submit an access request."""
    _, _, _, _, from_addr = _smtp_config()
    name = user_data.get("first_name") or user_data.get("name") or "there"
    msg = MIMEMultipart()
    msg["From"]    = from_addr
    msg["To"]      = to_email
    msg["Subject"] = "Your Access Request Has Been Received — FEMA Flood Certificate Generator"
    body = f"""\
Dear {name},

Thank you for submitting your access request for the FEMA Flood Certificate Generator platform.

Your request details:
  Name:            {user_data.get('first_name', '')} {user_data.get('last_name', '')}
  Company:         {user_data.get('company_name', '')}
  Email:           {to_email}

Your request has been received and is currently under review by our administrator.
You will be notified by email once your request has been approved or if additional
information is needed.

If you have any questions, please contact your platform administrator.

—
FEMA Flood Certificate Generator
"""
    msg.attach(MIMEText(body, "plain"))
    _send(msg)


def send_admin_access_notification(admin_email: str, user_data: dict) -> None:
    """Notification to admin when a new access request is submitted."""
    _, _, _, _, from_addr = _smtp_config()
    msg = MIMEMultipart()
    msg["From"]    = from_addr
    msg["To"]      = admin_email
    msg["Subject"] = f"New Access Request — {user_data.get('first_name', '')} {user_data.get('last_name', '')} ({user_data.get('company_name', '')})"
    body = f"""\
A new access request has been submitted on the FEMA Flood Certificate Generator platform.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  First Name:      {user_data.get('first_name', '')}
  Last Name:       {user_data.get('last_name', '')}
  Company:         {user_data.get('company_name', '')}
  Company Address: {user_data.get('company_address', '')}
  Email:           {user_data.get('email', '')}
  Contact Number:  {user_data.get('contact_number', '')}
  Submitted At:    {user_data.get('created_at', '')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Please log in to the Admin Panel to review and approve or reject this request.

—
FEMA Flood Certificate Generator | Admin Notification
"""
    msg.attach(MIMEText(body, "plain"))
    _send(msg)


def send_welcome_email(to_email: str, name: str, company_name: str, temp_password: str) -> None:
    """Welcome email sent to user after their access request is approved."""
    _, _, _, _, from_addr = _smtp_config()
    msg = MIMEMultipart()
    msg["From"]    = from_addr
    msg["To"]      = to_email
    msg["Subject"] = "Welcome — Your Access Has Been Approved | FEMA Flood Certificate Generator"
    body = f"""\
Dear {name},

Your access request for the FEMA Flood Certificate Generator has been approved!

You can now log in using the credentials below:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Platform URL:    (your platform URL)
  Email:           {to_email}
  Temporary Password: {temp_password}
  Company:         {company_name}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

IMPORTANT: Please change your password after your first login.

You will only see flood certificate history for your company ({company_name}).
Contact your administrator if you need access adjustments.

—
FEMA Flood Certificate Generator
"""
    msg.attach(MIMEText(body, "plain"))
    _send(msg)


def send_rejection_email(to_email: str, name: str) -> None:
    """Rejection email sent when an access request is not approved."""
    _, _, _, _, from_addr = _smtp_config()
    msg = MIMEMultipart()
    msg["From"]    = from_addr
    msg["To"]      = to_email
    msg["Subject"] = "Access Request Update — FEMA Flood Certificate Generator"
    body = f"""\
Dear {name},

Thank you for your interest in the FEMA Flood Certificate Generator platform.

After reviewing your access request, we are unable to approve access at this time.

If you believe this is in error or would like more information, please contact your
platform administrator directly.

—
FEMA Flood Certificate Generator
"""
    msg.attach(MIMEText(body, "plain"))
    _send(msg)


def send_lol_alert_email(
    lender_email: str,
    monitoring: dict,
    changed_items: list[dict],
) -> None:
    """LOL change-detection alert to lender.

    changed_items: list of {field, label, old_value, new_value}
    """
    _, _, _, _, from_addr = _smtp_config()
    prop_addr = monitoring.get("property_address", "")
    loan_id   = monitoring.get("loan_id", "")
    lender    = monitoring.get("lender_name", "Lender")

    msg = MIMEMultipart()
    msg["From"]    = from_addr
    msg["To"]      = lender_email
    msg["Subject"] = f"FLOOD ZONE CHANGE ALERT — Action Required: {prop_addr}"

    # Build a plain-text change table
    rows = []
    for item in changed_items:
        rows.append(
            f"  {item['label']:<35} {str(item['old_value']):<20} → {item['new_value']}"
        )
    change_table = "\n".join(rows) if rows else "  (see details above)"

    from datetime import datetime
    detected_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    body = f"""\
Dear {lender},

FLOOD ZONE CHANGE ALERT — ACTION REQUIRED

A change has been detected in the FEMA FIRM data for the monitored property listed below.
Under federal SFHDF regulations, a re-determination is required when FIRM map data changes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Property Address:  {prop_addr}
  Loan Reference:    {loan_id}
  Change Detected:   {detected_at}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CHANGED FIELDS:
  {"Field":<35} {"Previous Value":<20}   New Value
  {"-"*70}
{change_table}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ACTION REQUIRED:
Please log in to the FEMA Flood Certificate Generator and re-run a new flood
determination for this property immediately to ensure regulatory compliance.

If flood insurance is involved, verify current zone status with the borrower
and update your records accordingly.

—
FEMA Flood Certificate Generator | Life-of-Loan Monitoring Service
"""
    msg.attach(MIMEText(body, "plain"))
    _send(msg)


def send_password_reset_email(to_email: str, name: str, reset_url: str) -> None:
    """Password reset link email."""
    _, _, _, _, from_addr = _smtp_config()
    msg = MIMEMultipart()
    msg["From"]    = from_addr
    msg["To"]      = to_email
    msg["Subject"] = "Reset Your FloodCert AI Password"
    body = f"""Hello {name},

We received a request to reset your FloodCert AI password.

Click the link below to set a new password (valid for 1 hour):

{reset_url}

If you did not request a password reset, please ignore this email.

—
FloodCert AI | Automated Notification
"""
    msg.attach(MIMEText(body, "plain"))
    _send(msg)
