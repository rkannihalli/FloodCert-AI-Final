import os
import base64
import resend

resend.api_key = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "FloodCert AI <onboarding@resend.dev>")


def _send(to_email: str, subject: str, body: str, attachments: list | None = None) -> None:
    if not resend.api_key:
        print("[EMAIL] RESEND_API_KEY not set — email not sent. Add it to Railway environment variables.")
        raise ValueError("RESEND_API_KEY not configured. Set it as an environment variable.")
    payload = {"from": FROM_EMAIL, "to": [to_email], "subject": subject, "text": body}
    if attachments:
        payload["attachments"] = attachments
    try:
        resend.Emails.send(payload)
    except Exception as e:
        print(f"[EMAIL] Resend send failed to {to_email}: {e}")
        raise


def send_certificate_email(to_email: str, record: dict, pdf_bytes: bytes) -> None:
    subject = (
        f"Flood Hazard Determination — Loan {record['loan_id']} "
        f"({record.get('matched_address') or record.get('property_address', '')})"
    )
    sfha_line = (
        "⚠ This property IS in a Special Flood Hazard Area (SFHA). "
        "Federal flood insurance is required."
        if record.get("sfha_status") == "Yes"
        else "This property is NOT in a Special Flood Hazard Area."
    )
    body = f"""Dear {record['lender_name']},

Please find the attached Flood Hazard Determination Certificate for the following loan file.

Loan ID:            {record['loan_id']}
Borrower:           {record['borrower_name']}
Property:           {record.get('matched_address') or record.get('property_address', '')}
Flood Zone:         {record['flood_zone']}
SFHA Status:        {record['sfha_status']}
Insurance:          {record['insurance_required']}
Determination Date: {record['determination_date']}

{sfha_line}

The attached PDF is the official flood determination certificate.

Data sources: US Census Bureau Geocoder + FEMA National Flood Hazard Layer (NFHL).

—
FEMA Flood Certificate Generator
"""
    safe_loan = record["loan_id"].replace(" ", "_").replace("/", "-")
    attachments = [{
        "filename": f"flood_certificate_{safe_loan}.pdf",
        "content": base64.b64encode(pdf_bytes).decode("utf-8"),
    }]
    _send(to_email, subject, body, attachments)


def send_redetermination_notification(record: dict) -> None:
    to_email = (record.get("lender_email") or "").strip()
    if not to_email:
        raise ValueError("No lender email on record.")
    prop_addr = record.get("matched_address") or record.get("property_address", "")
    subject = f"⚠ FIRM Map Change Detected — Re-Determination Required | Loan {record['loan_id']} | {prop_addr}"
    body = f"""Dear {record['lender_name']},

A change to the FEMA Flood Insurance Rate Map (FIRM) panel covering the property below
has been detected during automated Life-of-Loan monitoring.

Loan ID:             {record['loan_id']}
Borrower:            {record['borrower_name']}
Property:            {prop_addr}
Original Flood Zone: {record.get('flood_zone', 'N/A')}
Original Map Panel:  {record.get('community_number', 'N/A')}
Original Panel Date: {record.get('panel_effective_date', 'N/A')}

ACTION REQUIRED: Please re-run the flood determination for this property.

—
FEMA Flood Certificate Generator | Life-of-Loan Monitoring
"""
    _send(to_email, subject, body)


def send_access_request_confirmation(to_email: str, user_data: dict) -> None:
    name = user_data.get("first_name") or user_data.get("name") or "there"
    subject = "Your Access Request Has Been Received — FEMA Flood Certificate Generator"
    body = f"""Dear {name},

Thank you for submitting your access request for the FEMA Flood Certificate Generator platform.

Name:    {user_data.get('first_name', '')} {user_data.get('last_name', '')}
Company: {user_data.get('company_name', '')}
Email:   {to_email}

Your request has been received and is currently under review by our administrator.
You will be notified by email once your request has been approved or if additional
information is needed.

—
FEMA Flood Certificate Generator
"""
    _send(to_email, subject, body)


def send_admin_access_notification(admin_email: str, user_data: dict) -> None:
    subject = (
        f"New Access Request — {user_data.get('first_name', '')} "
        f"{user_data.get('last_name', '')} ({user_data.get('company_name', '')})"
    )
    body = f"""A new access request has been submitted on the FEMA Flood Certificate Generator platform.

First Name:      {user_data.get('first_name', '')}
Last Name:       {user_data.get('last_name', '')}
Company:         {user_data.get('company_name', '')}
Company Address: {user_data.get('company_address', '')}
Email:           {user_data.get('email', '')}
Contact Number:  {user_data.get('contact_number', '')}
Submitted At:    {user_data.get('created_at', '')}

Please log in to the Admin Panel to review and approve or reject this request.

—
FEMA Flood Certificate Generator | Admin Notification
"""
    _send(admin_email, subject, body)


def send_welcome_email(to_email: str, name: str, company_name: str, temp_password: str) -> None:
    subject = "Welcome — Your Access Has Been Approved | FEMA Flood Certificate Generator"
    body = f"""Dear {name},

Your access request for the FEMA Flood Certificate Generator has been approved!

Email:              {to_email}
Temporary Password: {temp_password}
Company:            {company_name}

IMPORTANT: Please change your password after your first login.

—
FEMA Flood Certificate Generator
"""
    _send(to_email, subject, body)


def send_rejection_email(to_email: str, name: str) -> None:
    subject = "Access Request Update — FEMA Flood Certificate Generator"
    body = f"""Dear {name},

Thank you for your interest in the FEMA Flood Certificate Generator platform.

After reviewing your access request, we are unable to approve access at this time.

If you believe this is in error, please contact your platform administrator directly.

—
FEMA Flood Certificate Generator
"""
    _send(to_email, subject, body)


def send_lol_alert_email(lender_email: str, monitoring: dict, changed_items: list[dict]) -> None:
    from datetime import datetime
    prop_addr = monitoring.get("property_address", "")
    loan_id   = monitoring.get("loan_id", "")
    lender    = monitoring.get("lender_name", "Lender")
    subject = f"FLOOD ZONE CHANGE ALERT — Action Required: {prop_addr}"
    rows = "\n".join(
        f"  {i['label']:<35} {str(i['old_value']):<20} -> {i['new_value']}" for i in changed_items
    ) or "  (see details above)"
    detected_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    body = f"""Dear {lender},

FLOOD ZONE CHANGE ALERT — ACTION REQUIRED

Property Address: {prop_addr}
Loan Reference:    {loan_id}
Change Detected:   {detected_at}

CHANGED FIELDS:
{rows}

ACTION REQUIRED:
Please log in to the FEMA Flood Certificate Generator and re-run a new flood
determination for this property immediately to ensure regulatory compliance.

—
FEMA Flood Certificate Generator | Life-of-Loan Monitoring Service
"""
    _send(lender_email, subject, body)


def send_password_reset_email(to_email: str, name: str, reset_url: str) -> None:
    subject = "Reset Your FloodCert AI Password"
    body = f"""Hello {name},

We received a request to reset your FloodCert AI password.

Click the link below to set a new password (valid for 1 hour):

{reset_url}

If you did not request a password reset, please ignore this email.

—
FloodCert AI | Automated Notification
"""
    _send(to_email, subject, body)
