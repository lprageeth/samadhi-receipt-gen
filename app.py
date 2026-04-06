import os
import re
import json
import io
import base64
import datetime as dt
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from dateutil import tz
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


# -----------------------------
# Apps Script (Google Sheet log + receipt numbering)
# -----------------------------
USE_GOOGLE_SHEET = True  # set False to go back to local CSV/JSON

# Move sensitive values to Streamlit secrets:
# - Local: .streamlit/secrets.toml
# - Streamlit Cloud: App Settings -> Secrets
APPS_SCRIPT_URL = st.secrets["APPS_SCRIPT_URL"]
APPS_SCRIPT_TOKEN = st.secrets["APPS_SCRIPT_TOKEN"]




# -----------------------------
# Authorized Officers (Editable List)
# -----------------------------
AUTHORIZED_OFFICERS = [
    {
        "label": "Delon Reyhart – Treasurer",
        "name": "Delon Reyhart",
        "title": "Treasurer"
    },
    {
        "label": "Sasith Rajasooriya – President",
        "name": "Sasith Rajasooriya",
        "title": "President"
    },
]

# ==========================================================
# CONFIGURATION SECTION (Edit here without touching logic)
# ==========================================================

PURPOSE_OPTIONS = [
    "Membership Contribution",
    "General Donation ",
    "Project Donation ",
    "Event / Fundraiser",
    "Other",
]

PAYMENT_METHODS = [
    "Zelle",
    "Check",
    "Cash",
    "Wise",
    "Bank Transfer",
    "Credit Card",
    "Other",
]

MEMBERSHIP_TIERS = [
    "Daana Samadhi Member",
    "Pathway Samadhi Member",
    "Lotus Samadhi Member",
]

# --- Optional: load editable settings from settings.json (if present) ---
# This lets you change officers/options/tiers without touching code.
try:
    _settings_path = Path(__file__).parent / "settings.json"
    if _settings_path.exists():
        _settings = json.loads(_settings_path.read_text(encoding="utf-8"))

        if isinstance(_settings.get("AUTHORIZED_OFFICERS"), list) and _settings["AUTHORIZED_OFFICERS"]:
            AUTHORIZED_OFFICERS = _settings["AUTHORIZED_OFFICERS"]

        if isinstance(_settings.get("PURPOSE_OPTIONS"), list) and _settings["PURPOSE_OPTIONS"]:
            PURPOSE_OPTIONS = _settings["PURPOSE_OPTIONS"]

        if isinstance(_settings.get("PAYMENT_METHODS"), list) and _settings["PAYMENT_METHODS"]:
            PAYMENT_METHODS = _settings["PAYMENT_METHODS"]

        if isinstance(_settings.get("MEMBERSHIP_TIERS"), list) and _settings["MEMBERSHIP_TIERS"]:
            MEMBERSHIP_TIERS = _settings["MEMBERSHIP_TIERS"]
except Exception:
    # If settings.json is malformed, fall back to the defaults above.
    pass

# Default description templates
# Keep keys exactly matching your current settings.json values.
DESCRIPTION_TEMPLATES = {
    "Membership Contribution": "{tier} contribution for {year}.",
    "Project Donation ": "Donation designated for {project} project.",
    "General Donation ": "Unrestricted charitable contribution.",
    "Event / Fundraiser": "Charitable contribution in support of our event/fundraiser.",
}

# -----------------------------
# Organization config
# -----------------------------
ORG_NAME = "SAMADHI FOUNDATION"
ORG_EIN = "41-2428230"
ORG_ADDRESS_LINES = [
    "1144 Autumn Ridge Dr",
    "Lebanon, OH 45036",
]
DEFAULT_PREFIX = "SF"

AUTHORIZED_NAME_DEFAULT = "Delon Reyhart"
AUTHORIZED_TITLE_DEFAULT = "Treasurer"
AUTHORIZED_ORG_LINE = "Samadhi Foundation"

SIGNATURE_STYLE = "ttf"
SIGNATURE_TTF_PATH = "fonts/Signature.ttf"
SIGNATURE_FONT_NAME = "SigFont"

# -----------------------------
# Assets
# -----------------------------
BASE_DIR = Path(__file__).parent.resolve()
LOGO_PATH = BASE_DIR / "assets" / "logo.png"

# -----------------------------
# Storage paths (local fallback only)
# -----------------------------
DATA_DIR = BASE_DIR / "receipts_data"
PDF_DIR = DATA_DIR / "pdf"
LOG_PATH = DATA_DIR / "receipt_log.csv"
SEQ_PATH = DATA_DIR / "receipt_sequence.json"

for d in [DATA_DIR, PDF_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# -----------------------------
# Helpers
# -----------------------------
# def gs_post(payload: dict, timeout: int = 60) -> dict:
#     r = requests.post(
#         APPS_SCRIPT_URL,
#         json={**payload, "token": APPS_SCRIPT_TOKEN},
#         timeout=timeout,
#     )
#     r.raise_for_status()
#     data = r.json()
#     if not data.get("ok"):
#         raise RuntimeError(f"Apps Script error: {data}")
#     return data

def gs_post(payload: dict, timeout: int = 60) -> dict:
    r = requests.post(
        APPS_SCRIPT_URL,
        json={**payload, "token": APPS_SCRIPT_TOKEN},
        timeout=timeout,
    )

    # st.write("HTTP status:", r.status_code)
    # st.write("Content-Type:", r.headers.get("content-type"))
    # st.code(r.text[:1000])

    r.raise_for_status()

    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Apps Script error: {data}")
    return data

def gs_upload_pdf(receipt_id: str, filename: str, pdf_bytes: bytes) -> dict:
    return gs_post(
        {
            "action": "upload_pdf",
            "receipt_id": receipt_id,
            "filename": filename,
            "pdf_base64": base64.b64encode(pdf_bytes).decode("utf-8"),
        },
        timeout=60,
    )


def gs_next_receipt_id(year: int) -> str:
    data = gs_post(
        {"action": "next_receipt_id", "year": int(year)},
        timeout=30,
    )
    return data["receipt_id"]


def gs_append_receipt(row: dict) -> None:
    gs_post(
        {"action": "append_receipt", "row": row},
        timeout=30,
    )


def gs_send_receipt_email(
    donor_name: str,
    donor_email: str,
    receipt_id: str,
    amount_usd: float,
    date_received: str,
    pdf_filename: str,
    pdf_bytes: bytes,
) -> dict:
    return gs_post(
        {
            "action": "send_receipt_email",
            "donor_name": donor_name,
            "donor_email": donor_email,
            "receipt_id": receipt_id,
            "amount_usd": amount_usd,
            "date_received": date_received,
            "filename": pdf_filename,
            "pdf_base64": base64.b64encode(pdf_bytes).decode("utf-8"),
        },
        timeout=90,
    )


def today_local() -> dt.date:
    return dt.datetime.now(tz.tzlocal()).date()


def sanitize_filename(s: str) -> str:
    s = s.strip().replace(" ", "_")
    s = re.sub(r"[^A-Za-z0-9_\-\.]+", "", s)
    return s[:120] if len(s) > 120 else s


def load_sequence() -> dict:
    if SEQ_PATH.exists():
        return json.loads(SEQ_PATH.read_text())
    return {"year": None, "next": 1}


def save_sequence(seq: dict) -> None:
    SEQ_PATH.write_text(json.dumps(seq, indent=2))


def next_receipt_id(prefix: str, receipt_date: dt.date) -> str:
    year = receipt_date.year
    seq = load_sequence()
    if seq.get("year") != year:
        seq = {"year": year, "next": 1}
    n = int(seq["next"])
    seq["next"] = n + 1
    save_sequence(seq)
    return f"{prefix}-{year}-{n:04d}"


def ensure_log_exists() -> None:
    if not LOG_PATH.exists():
        df = pd.DataFrame(columns=[
            "receipt_id",
            "created_at_local",
            "donor_name",
            "donor_email",
            "donor_address",
            "amount_usd",
            "date_received",
            "payment_method",
            "purpose",
            "project_name",
            "description",
            "goods_services_provided",
            "goods_services_value_usd",
            "officer_name",
            "officer_title",
            "notes_internal",
            "pdf_file",
            "status",
            "void_reason",
            "reissue_of_receipt_id",
            "replaced_by_receipt_id",
            "email_sent",
            "email_error",
        ])
        df.to_csv(LOG_PATH, index=False)


def append_log(row: dict) -> None:
    ensure_log_exists()
    df = pd.read_csv(LOG_PATH)
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(LOG_PATH, index=False)


def make_receipt_pdf(
    receipt_id: str,
    created_at_str: str,
    donor_name: str,
    donor_address: str,
    donor_email: str,
    amount_usd: float,
    date_received: dt.date,
    payment_method: str,
    purpose: str,
    project_name: str,
    description: str,
    goods_services_provided: bool,
    goods_services_value_usd: float,
    authorized_name: str,
    authorized_title: str,
):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    width, height = LETTER

    left = 0.85 * inch
    right = width - 0.85 * inch
    y = height - 0.85 * inch
    line_h = 13

    def draw_left(text, y_pos, font="Helvetica", size=11):
        c.setFont(font, size)
        c.drawString(left, y_pos, text)

    def hr(y_pos):
        c.setLineWidth(0.8)
        c.line(left, y_pos, right, y_pos)

    # -----------------------------
    # 1) Letterhead with logo
    # -----------------------------
    logo_width = 1.5 * inch
    logo_height = 1.5 * inch
    text_x = left
    logo_x = left - 30
    logo_y = y - logo_height + 42
    if LOGO_PATH.exists():
        try:
            logo = ImageReader(str(LOGO_PATH))
            c.drawImage(
                logo,
                logo_x,
                logo_y,
                width=logo_width,
                height=logo_height,
                preserveAspectRatio=True,
                mask="auto",
            )
            text_x = left + logo_width-40 
        except Exception:
            text_x = left

    c.setFont("Helvetica-Bold", 16)
    c.drawString(text_x, y, ORG_NAME)
    y -= 0.28 * inch

    c.setFont("Helvetica", 10.8)
    for addr in ORG_ADDRESS_LINES:
        c.drawString(text_x, y, addr)
        y -= line_h
    c.drawString(text_x, y, f"EIN: {ORG_EIN}")
    y -= 0.20 * inch

    y -= 0.35 * inch

    # -----------------------------
    # Title (centered) + divider
    # -----------------------------
    c.setFont("Helvetica-Bold", 15)
    title_text = "DONATION RECEIPT"
    title_w = c.stringWidth(title_text, "Helvetica-Bold", 15)
    c.drawString((width - title_w) / 2, y, title_text)

    y -= 0.26 * inch
    c.setLineWidth(1)
    c.line(left, y, right, y)
    y -= 0.28 * inch

    # -----------------------------
    # Receipt info
    # -----------------------------
    c.setFont("Helvetica-Bold", 12)
    c.drawString(left, y, "Receipt Information")
    y -= 0.22 * inch
    c.setFont("Helvetica", 11)
    draw_left(f"Receipt Number: {receipt_id}", y); y -= line_h
    draw_left(f"Date Received: {date_received.strftime('%Y-%m-%d')}", y); y -= line_h
    draw_left(f"Payment Method: {payment_method}", y); y -= line_h
    draw_left(f"Created: {created_at_str}", y); y -= line_h

    y -= 0.10 * inch
    c.line(left, y, right, y)
    y -= 0.30 * inch

    # -----------------------------
    # Donor Information
    # -----------------------------
    c.setFont("Helvetica-Bold", 12)
    c.drawString(left, y, "Donor Information")
    y -= 0.22 * inch

    c.setFont("Helvetica", 11)
    draw_left(f"Name: {donor_name}", y); y -= line_h

    if donor_email.strip():
        draw_left(f"Email: {donor_email.strip()}", y); y -= line_h

    if donor_address.strip():
        addr_lines = [ln.strip() for ln in donor_address.splitlines() if ln.strip()]
        if addr_lines:
            draw_left("Address:", y); y -= line_h
            for ln in addr_lines:
                draw_left(f"  {ln}", y); y -= line_h

    y -= 0.12 * inch
    hr(y)
    y -= 0.30 * inch

    # -----------------------------
    # Contribution Details
    # -----------------------------
    c.setFont("Helvetica-Bold", 12)
    c.drawString(left, y, "Contribution Details")
    y -= 0.22 * inch

    c.setFont("Helvetica", 11)
    draw_left(f"Amount: ${amount_usd:,.2f} USD", y); y -= line_h
    draw_left(f"Purpose: {purpose}", y); y -= line_h
    if project_name.strip():
        draw_left(f"Project: {project_name.strip()}", y); y -= line_h

    desc = description.strip()
    if desc:
        y -= 0.06 * inch
        c.setFont("Helvetica-Bold", 11)
        c.drawString(left, y, "Description:")
        y -= 0.18 * inch
        c.setFont("Helvetica", 11)

        max_chars = 92
        for i in range(0, len(desc), max_chars):
            c.drawString(left, y, desc[i:i + max_chars])
            y -= line_h

    y -= 0.10 * inch
    hr(y)
    y -= 0.30 * inch

    # -----------------------------
    # Acknowledgment
    # -----------------------------
    c.setFont("Helvetica-Bold", 12)
    c.drawString(left, y, "Acknowledgment")
    y -= 0.22 * inch

    c.setFont("Helvetica", 11)
    if goods_services_provided:
        c.drawString(left, y, "Goods or services were provided in exchange for this contribution.")
        y -= line_h
        c.drawString(left, y, f"Estimated value of goods/services: ${goods_services_value_usd:,.2f} USD")
        y -= line_h
    else:
        c.drawString(left, y, "No goods or services were provided in exchange for this contribution.")
        y -= line_h

    # -----------------------------
    # Signature block
    # -----------------------------
    sig_y = 2.25 * inch

    c.setFont("Helvetica", 11)
    c.drawString(left, sig_y + 0.72 * inch, "Issued by:")

    sig_line_x0 = left + 1.35 * inch
    sig_line_y = sig_y + 0.66 * inch
    c.setLineWidth(0.8)
    c.line(sig_line_x0, sig_line_y, right, sig_line_y)

    name = (authorized_name or "").strip()
    title = (authorized_title or "Treasurer").strip()

    sig_font_loaded = False
    if SIGNATURE_STYLE == "ttf" and name:
        try:
            if os.path.exists(SIGNATURE_TTF_PATH):
                try:
                    pdfmetrics.getFont(SIGNATURE_FONT_NAME)
                except KeyError:
                    pdfmetrics.registerFont(TTFont(SIGNATURE_FONT_NAME, SIGNATURE_TTF_PATH))
                sig_font_loaded = True
        except Exception:
            sig_font_loaded = False

    if name:
        if sig_font_loaded:
            c.setFont(SIGNATURE_FONT_NAME, 22)
        else:
            c.setFont("Helvetica-Oblique", 16)

        sig_text_y = sig_line_y + 0.1 * inch
        c.drawString(sig_line_x0, sig_text_y, name)

    c.setFont("Helvetica", 10.8)
    c.drawString(sig_line_x0, sig_line_y - 0.18 * inch, title)
    c.drawString(sig_line_x0, sig_line_y - 0.36 * inch, "Samadhi Foundation")

    c.showPage()
    c.save()
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes


# -----------------------------
# Templates
# -----------------------------
def default_description(purpose: str, tier: str, year: int, project_name: str) -> str:
    template = DESCRIPTION_TEMPLATES.get(purpose, "")
    if not template:
        return ""

    return template.format(
        tier=tier.strip() or "Membership",
        year=year,
        project=project_name.strip() or "the designated project",
    )


# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="Samadhi Receipt Generator", layout="centered")
st.title("Samadhi Foundation – Receipt Generator")
st.caption("Generate a receipt PDF, save it to Google Drive, and optionally email it to the donor.")

with st.expander("Organization details on receipts"):
    st.write(f"**{ORG_NAME}**")
    st.write(f"EIN: **{ORG_EIN}**")
    st.write("Address:")
    for line in ORG_ADDRESS_LINES:
        st.write(f"- {line}")

st.divider()

colA, colB = st.columns(2)
with colA:
    receipt_prefix = st.text_input("Receipt ID Prefix", value=DEFAULT_PREFIX)
with colB:
    date_received = st.date_input("Date Received", value=today_local())

st.subheader("Donor")
donor_name = st.text_input("Donor Name", value="")
donor_email = st.text_input("Donor Email (optional)", value="")
donor_address = st.text_area("Donor Address (optional)", value="", height=90)

st.subheader("Contribution")
amount_usd = st.number_input("Amount (USD)", min_value=0.0, step=1.0, value=0.0, format="%.2f")
payment_method = st.selectbox("Payment Method", PAYMENT_METHODS, index=0)
purpose = st.selectbox("Purpose", PURPOSE_OPTIONS, index=0)

tier = ""
project_name = ""

if purpose == "Membership Contribution":
    tier_options = [""] + MEMBERSHIP_TIERS + ["Other"]

    tier = st.selectbox(
        "Membership Tier",
        tier_options,
        index=0
    )
    if tier == "Other":
        tier = st.text_input("Custom Tier Name", value="")
elif purpose == "Project Donation ":
    project_name = st.text_input("Project Name", value="")

auto_desc = default_description(purpose, tier, date_received.year, project_name)
description = st.text_area("Receipt Description (editable)", value=auto_desc, height=80)

st.subheader("Acknowledgment")
goods_services_provided = st.checkbox("Goods/services were provided in exchange for this contribution", value=False)
goods_services_value_usd = 0.0
if goods_services_provided:
    goods_services_value_usd = st.number_input("Estimated value (USD)", min_value=0.0, step=1.0, value=0.0, format="%.2f")

st.subheader("Authorized officer (printed on receipt)")

officer_labels = [o["label"] for o in AUTHORIZED_OFFICERS]

selected_label = st.selectbox(
    "Issuing Officer",
    officer_labels,
    index=0
)

selected_officer = next(o for o in AUTHORIZED_OFFICERS if o["label"] == selected_label)

authorized_name = selected_officer["name"]
authorized_title = selected_officer["title"]

st.subheader("Email")
email_receipt = st.checkbox("Email receipt to donor automatically", value=True)

st.subheader("Internal / audit fields")
status = st.selectbox(
    "Status",
    ["ISSUED", "VOIDED", "CORRECTED", "REISSUED"],
    index=0,
    help="Use ISSUED for normal receipts. Other values support audit tracking."
)
void_reason = st.text_input("Void / correction reason (optional)", value="")
reissue_of_receipt_id = st.text_input("Reissue of receipt ID (optional)", value="")
replaced_by_receipt_id = st.text_input("Replaced by receipt ID (optional)", value="")
notes_internal = st.text_area("Internal Notes", value="", height=80)

st.divider()

generate = st.button("Generate Receipt PDF", type="primary")

if generate:
    errors = []
    if not receipt_prefix.strip():
        errors.append("Receipt prefix is required.")
    if not donor_name.strip():
        errors.append("Donor name is required (use 'Anonymous' if needed).")
    if amount_usd <= 0:
        errors.append("Amount must be greater than 0.")
    if email_receipt and not donor_email.strip():
        errors.append("Donor email is required if automatic emailing is enabled.")
    if status == "VOIDED" and not void_reason.strip():
        errors.append("Please provide a void / correction reason when status is VOIDED.")

    if errors:
        st.error("Please fix:\n- " + "\n- ".join(errors))
    else:
        drive_file_id = ""
        drive_file_url = ""
        email_sent = False
        email_error = ""

        try:
            if USE_GOOGLE_SHEET:
                receipt_id = gs_next_receipt_id(date_received.year)
            else:
                receipt_id = next_receipt_id(receipt_prefix.strip(), date_received)

            created_at = dt.datetime.now(tz.tzlocal()).strftime("%Y-%m-%d %H:%M:%S %Z")
            pdf_name = f"{sanitize_filename(receipt_id)}.pdf"

            pdf_bytes = make_receipt_pdf(
                receipt_id=receipt_id,
                created_at_str=created_at,
                donor_name=donor_name.strip(),
                donor_address=donor_address.strip(),
                donor_email=donor_email.strip(),
                amount_usd=float(amount_usd),
                date_received=date_received,
                payment_method=payment_method,
                purpose=purpose,
                project_name=project_name.strip(),
                description=description.strip(),
                goods_services_provided=bool(goods_services_provided),
                goods_services_value_usd=float(goods_services_value_usd),
                authorized_name=authorized_name.strip(),
                authorized_title=authorized_title.strip(),
            )

            log_row = {
                "receipt_id": receipt_id,
                "date_received": date_received.strftime("%Y-%m-%d"),
                "created_timestamp": created_at,
                "donor_name": donor_name.strip(),
                "donor_email": donor_email.strip(),
                "donor_address": donor_address.strip(),
                "amount_usd": float(amount_usd),
                "payment_method": payment_method,
                "purpose": purpose,
                "project_name": project_name.strip(),
                "description": description.strip(),
                "goods_services_provided": bool(goods_services_provided),
                "goods_services_value_usd": float(goods_services_value_usd),
                "officer_name": authorized_name.strip(),
                "officer_title": authorized_title.strip(),
                "pdf_filename": pdf_name,
                "status": status,
                "void_reason": void_reason.strip(),
                "reissue_of_receipt_id": reissue_of_receipt_id.strip(),
                "replaced_by_receipt_id": replaced_by_receipt_id.strip(),
                "notes_internal": notes_internal.strip(),
                "email_sent": False,
                "email_error": "",
            }

            if USE_GOOGLE_SHEET:
                drive_info = gs_upload_pdf(receipt_id, pdf_name, pdf_bytes)
                drive_file_id = drive_info.get("file_id", "")
                drive_file_url = drive_info.get("file_url", "")

                log_row["drive_file_id"] = drive_file_id
                log_row["drive_file_url"] = drive_file_url

                # Only auto-email normally issued / corrected / reissued receipts.
                if email_receipt and donor_email.strip() and status != "VOIDED":
                    try:
                        gs_send_receipt_email(
                            donor_name=donor_name.strip(),
                            donor_email=donor_email.strip(),
                            receipt_id=receipt_id,
                            amount_usd=float(amount_usd),
                            date_received=date_received.strftime("%Y-%m-%d"),
                            pdf_filename=pdf_name,
                            pdf_bytes=pdf_bytes,
                        )
                        email_sent = True
                        log_row["email_sent"] = True
                    except Exception as e:
                        email_error = str(e)
                        log_row["email_error"] = email_error

                gs_append_receipt(log_row)

            else:
                local_pdf_path = PDF_DIR / pdf_name
                with open(local_pdf_path, "wb") as f:
                    f.write(pdf_bytes)

                if email_receipt:
                    email_error = "Automatic email is only implemented in Google mode."
                    log_row["email_error"] = email_error

                append_log({
                    "receipt_id": receipt_id,
                    "created_at_local": created_at,
                    "donor_name": donor_name.strip(),
                    "donor_email": donor_email.strip(),
                    "donor_address": donor_address.strip(),
                    "amount_usd": float(amount_usd),
                    "date_received": date_received.strftime("%Y-%m-%d"),
                    "payment_method": payment_method,
                    "purpose": purpose,
                    "project_name": project_name.strip(),
                    "description": description.strip(),
                    "goods_services_provided": bool(goods_services_provided),
                    "goods_services_value_usd": float(goods_services_value_usd),
                    "officer_name": authorized_name.strip(),
                    "officer_title": authorized_title.strip(),
                    "notes_internal": notes_internal.strip(),
                    "pdf_file": pdf_name,
                    "status": status,
                    "void_reason": void_reason.strip(),
                    "reissue_of_receipt_id": reissue_of_receipt_id.strip(),
                    "replaced_by_receipt_id": replaced_by_receipt_id.strip(),
                    "email_sent": email_sent,
                    "email_error": email_error,
                })

            st.success(f"Receipt created: {receipt_id}")

            if drive_file_url:
                st.caption("Saved to Google Drive.")
                st.markdown(f"[Open in Drive]({drive_file_url})")

            if email_receipt and status != "VOIDED":
                if email_sent:
                    st.success(f"Receipt email sent to {donor_email.strip()}")
                elif email_error:
                    st.warning(f"Receipt created, but email was not sent: {email_error}")
            elif email_receipt and status == "VOIDED":
                st.info("Automatic donor email was skipped because status is VOIDED.")

            st.download_button(
                "Download Receipt PDF",
                data=pdf_bytes,
                file_name=pdf_name,
                mime="application/pdf",
            )

            if not USE_GOOGLE_SHEET:
                df = pd.read_csv(LOG_PATH)
                st.write("### Recent receipts")
                st.dataframe(df.tail(15), use_container_width=True)
            else:
                st.info("Logged to Google Sheet (central log).")

        except Exception as e:
            st.error(f"Failed to create receipt: {e}")
