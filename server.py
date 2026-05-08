#!/usr/bin/env python3
"""
Naija Invoice - Invoicing tool for Nigerian businesses
Run: python3 server.py
Then open http://localhost:8080 in your browser
"""

import http.server
import json
import sqlite3
import os
import uuid
import base64
import mimetypes
import urllib.parse
import smtplib
import threading
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

PORT = 8080
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "invoices.db")
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

VAT_RATE = 7.5  # Nigerian VAT rate


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            company_name TEXT DEFAULT '',
            company_address TEXT DEFAULT '',
            company_email TEXT DEFAULT '',
            company_phone TEXT DEFAULT '',
            company_logo TEXT DEFAULT '',
            bank_name TEXT DEFAULT '',
            account_name TEXT DEFAULT '',
            account_number TEXT DEFAULT '',
            smtp_host TEXT DEFAULT '',
            smtp_port INTEGER DEFAULT 587,
            smtp_user TEXT DEFAULT '',
            smtp_password TEXT DEFAULT '',
            smtp_from_name TEXT DEFAULT '',
            smtp_from_email TEXT DEFAULT '',
            default_reminder_days TEXT DEFAULT '3,7,14',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS clients (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT DEFAULT '',
            address TEXT DEFAULT '',
            company TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id TEXT PRIMARY KEY,
            invoice_number TEXT UNIQUE NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('invoice', 'quote')),
            status TEXT DEFAULT 'draft' CHECK (status IN ('draft', 'sent', 'paid', 'overdue', 'cancelled', 'accepted', 'declined')),
            client_id TEXT NOT NULL,
            issue_date TEXT NOT NULL,
            due_date TEXT NOT NULL,
            subtotal REAL DEFAULT 0,
            vat_amount REAL DEFAULT 0,
            discount_percent REAL DEFAULT 0,
            discount_amount REAL DEFAULT 0,
            total REAL DEFAULT 0,
            notes TEXT DEFAULT '',
            terms TEXT DEFAULT '',
            currency TEXT DEFAULT 'NGN',
            reminder_enabled INTEGER DEFAULT 0,
            reminder_frequency_days INTEGER DEFAULT 7,
            reminder_max_count INTEGER DEFAULT 3,
            reminder_sent_count INTEGER DEFAULT 0,
            last_reminder_sent TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (client_id) REFERENCES clients(id)
        );

        CREATE TABLE IF NOT EXISTS invoice_items (
            id TEXT PRIMARY KEY,
            invoice_id TEXT NOT NULL,
            description TEXT NOT NULL,
            quantity REAL DEFAULT 1,
            unit_price REAL DEFAULT 0,
            amount REAL DEFAULT 0,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS email_log (
            id TEXT PRIMARY KEY,
            invoice_id TEXT NOT NULL,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            type TEXT DEFAULT 'invoice',
            status TEXT DEFAULT 'sent',
            sent_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (invoice_id) REFERENCES invoices(id)
        );

        INSERT OR IGNORE INTO settings (id) VALUES (1);
    """)
    conn.commit()
    conn.close()


def generate_invoice_number(inv_type):
    conn = get_db()
    prefix = "INV" if inv_type == "invoice" else "QT"
    year = datetime.now().strftime("%Y")
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM invoices WHERE type = ? AND invoice_number LIKE ?",
        (inv_type, f"{prefix}-{year}-%")
    ).fetchone()
    count = row["cnt"] + 1
    conn.close()
    return f"{prefix}-{year}-{count:04d}"


def calculate_totals(items, discount_percent=0):
    subtotal = sum(item["quantity"] * item["unit_price"] for item in items)
    discount_amount = subtotal * (discount_percent / 100)
    taxable = subtotal - discount_amount
    vat_amount = taxable * (VAT_RATE / 100)
    total = taxable + vat_amount
    return {
        "subtotal": round(subtotal, 2),
        "discount_amount": round(discount_amount, 2),
        "vat_amount": round(vat_amount, 2),
        "total": round(total, 2),
    }


def format_naira(amount):
    return f"₦{amount:,.2f}"


def generate_invoice_html(invoice_id):
    conn = get_db()
    inv = dict(conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone())
    client = dict(conn.execute("SELECT * FROM clients WHERE id = ?", (inv["client_id"],)).fetchone())
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY sort_order", (invoice_id,)
    ).fetchall()]
    settings = dict(conn.execute("SELECT * FROM settings WHERE id = 1").fetchone())
    conn.close()

    logo_html = ""
    if settings["company_logo"]:
        logo_path = os.path.join(UPLOAD_DIR, settings["company_logo"])
        if os.path.exists(logo_path):
            mime = mimetypes.guess_type(logo_path)[0] or "image/png"
            with open(logo_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            logo_html = f'<img src="data:{mime};base64,{b64}" style="max-height:80px;max-width:200px;" />'

    type_label = "INVOICE" if inv["type"] == "invoice" else "QUOTATION"
    status_color = {
        "draft": "#6b7280", "sent": "#2563eb", "paid": "#16a34a",
        "overdue": "#dc2626", "cancelled": "#9ca3af", "accepted": "#16a34a", "declined": "#dc2626"
    }.get(inv["status"], "#6b7280")

    items_html = ""
    for i, item in enumerate(items):
        items_html += f"""
        <tr>
            <td style="padding:10px;border-bottom:1px solid #e5e7eb;">{i+1}</td>
            <td style="padding:10px;border-bottom:1px solid #e5e7eb;">{item['description']}</td>
            <td style="padding:10px;border-bottom:1px solid #e5e7eb;text-align:center;">{item['quantity']}</td>
            <td style="padding:10px;border-bottom:1px solid #e5e7eb;text-align:right;">{format_naira(item['unit_price'])}</td>
            <td style="padding:10px;border-bottom:1px solid #e5e7eb;text-align:right;">{format_naira(item['amount'])}</td>
        </tr>"""

    discount_row = ""
    if inv["discount_percent"] > 0:
        discount_row = f"""
        <tr>
            <td style="padding:8px 10px;text-align:right;color:#6b7280;">Discount ({inv['discount_percent']}%)</td>
            <td style="padding:8px 10px;text-align:right;color:#dc2626;">-{format_naira(inv['discount_amount'])}</td>
        </tr>"""

    bank_info = ""
    if settings["bank_name"] or settings["account_number"]:
        bank_info = f"""
        <div style="margin-top:30px;padding:15px;background:#f0fdf4;border-radius:8px;border:1px solid #bbf7d0;">
            <h3 style="margin:0 0 10px;color:#16a34a;font-size:14px;">Payment Information</h3>
            <p style="margin:3px 0;font-size:13px;"><strong>Bank:</strong> {settings['bank_name']}</p>
            <p style="margin:3px 0;font-size:13px;"><strong>Account Name:</strong> {settings['account_name']}</p>
            <p style="margin:3px 0;font-size:13px;"><strong>Account Number:</strong> {settings['account_number']}</p>
        </div>"""

    notes_html = ""
    if inv["notes"]:
        notes_html = '<div style="margin-top:20px;padding:15px;background:#f9fafb;border-radius:8px;"><p style="margin:0 0 5px;font-size:12px;color:#9ca3af;text-transform:uppercase;">Notes</p><p style="margin:0;font-size:13px;">' + inv["notes"] + '</p></div>'

    terms_html = ""
    if inv["terms"]:
        terms_html = '<div style="margin-top:15px;padding:15px;background:#f9fafb;border-radius:8px;"><p style="margin:0 0 5px;font-size:12px;color:#9ca3af;text-transform:uppercase;">Terms &amp; Conditions</p><p style="margin:0;font-size:13px;">' + inv["terms"] + '</p></div>'

    company_name = settings['company_name'] or 'Your Company'

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>{type_label} {inv['invoice_number']}</title></head>
<body style="font-family:'Segoe UI',Arial,sans-serif;max-width:800px;margin:0 auto;padding:40px;color:#1f2937;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:40px;">
        <div>
            {logo_html}
            <h2 style="margin:10px 0 5px;color:#111827;">{company_name}</h2>
            <p style="margin:2px 0;font-size:13px;color:#6b7280;">{settings['company_address']}</p>
            <p style="margin:2px 0;font-size:13px;color:#6b7280;">{settings['company_email']}</p>
            <p style="margin:2px 0;font-size:13px;color:#6b7280;">{settings['company_phone']}</p>
        </div>
        <div style="text-align:right;">
            <h1 style="margin:0;font-size:32px;color:#111827;">{type_label}</h1>
            <p style="margin:5px 0;font-size:16px;font-weight:600;color:#4b5563;">#{inv['invoice_number']}</p>
            <span style="display:inline-block;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600;color:white;background:{status_color};text-transform:uppercase;">{inv['status']}</span>
        </div>
    </div>

    <div style="display:flex;justify-content:space-between;margin-bottom:30px;">
        <div style="background:#f9fafb;padding:15px 20px;border-radius:8px;flex:1;margin-right:15px;">
            <p style="margin:0 0 5px;font-size:12px;color:#9ca3af;text-transform:uppercase;letter-spacing:1px;">Bill To</p>
            <p style="margin:3px 0;font-weight:600;">{client['name']}</p>
            <p style="margin:3px 0;font-size:13px;color:#6b7280;">{client.get('company','')}</p>
            <p style="margin:3px 0;font-size:13px;color:#6b7280;">{client['email']}</p>
            <p style="margin:3px 0;font-size:13px;color:#6b7280;">{client.get('address','')}</p>
        </div>
        <div style="background:#f9fafb;padding:15px 20px;border-radius:8px;min-width:200px;">
            <p style="margin:0 0 5px;font-size:12px;color:#9ca3af;text-transform:uppercase;letter-spacing:1px;">Details</p>
            <p style="margin:3px 0;font-size:13px;"><strong>Issue Date:</strong> {inv['issue_date']}</p>
            <p style="margin:3px 0;font-size:13px;"><strong>Due Date:</strong> {inv['due_date']}</p>
            <p style="margin:3px 0;font-size:13px;"><strong>Currency:</strong> {inv['currency']}</p>
        </div>
    </div>

    <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
        <thead>
            <tr style="background:#f9fafb;">
                <th style="padding:10px;text-align:left;font-size:12px;color:#6b7280;text-transform:uppercase;border-bottom:2px solid #e5e7eb;width:40px;">#</th>
                <th style="padding:10px;text-align:left;font-size:12px;color:#6b7280;text-transform:uppercase;border-bottom:2px solid #e5e7eb;">Description</th>
                <th style="padding:10px;text-align:center;font-size:12px;color:#6b7280;text-transform:uppercase;border-bottom:2px solid #e5e7eb;width:80px;">Qty</th>
                <th style="padding:10px;text-align:right;font-size:12px;color:#6b7280;text-transform:uppercase;border-bottom:2px solid #e5e7eb;width:120px;">Unit Price</th>
                <th style="padding:10px;text-align:right;font-size:12px;color:#6b7280;text-transform:uppercase;border-bottom:2px solid #e5e7eb;width:120px;">Amount</th>
            </tr>
        </thead>
        <tbody>{items_html}</tbody>
    </table>

    <div style="display:flex;justify-content:flex-end;">
        <table style="min-width:280px;">
            <tr>
                <td style="padding:8px 10px;text-align:right;color:#6b7280;">Subtotal</td>
                <td style="padding:8px 10px;text-align:right;font-weight:500;">{format_naira(inv['subtotal'])}</td>
            </tr>
            {discount_row}
            <tr>
                <td style="padding:8px 10px;text-align:right;color:#6b7280;">VAT ({VAT_RATE}%)</td>
                <td style="padding:8px 10px;text-align:right;font-weight:500;">{format_naira(inv['vat_amount'])}</td>
            </tr>
            <tr style="border-top:2px solid #111827;">
                <td style="padding:12px 10px;text-align:right;font-weight:700;font-size:16px;">Total</td>
                <td style="padding:12px 10px;text-align:right;font-weight:700;font-size:16px;color:#111827;">{format_naira(inv['total'])}</td>
            </tr>
        </table>
    </div>

    {bank_info}

    {notes_html}

    {terms_html}

    <div style="margin-top:40px;text-align:center;color:#9ca3af;font-size:11px;">
        <p>Generated by Naija Invoice | VAT compliant as per Nigerian tax law (FIRS)</p>
    </div>
</body>
</html>"""


def send_invoice_email(invoice_id, is_reminder=False):
    conn = get_db()
    inv = dict(conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone())
    client = dict(conn.execute("SELECT * FROM clients WHERE id = ?", (inv["client_id"],)).fetchone())
    settings = dict(conn.execute("SELECT * FROM settings WHERE id = 1").fetchone())
    conn.close()

    if not settings["smtp_host"] or not settings["smtp_user"]:
        return {"success": False, "error": "Email not configured. Go to Settings to set up SMTP."}

    type_label = "Invoice" if inv["type"] == "invoice" else "Quotation"

    if is_reminder:
        subject = f"Reminder: {type_label} #{inv['invoice_number']} - Payment Due"
        body_intro = f"This is a friendly reminder that {type_label.lower()} <strong>#{inv['invoice_number']}</strong> for <strong>{format_naira(inv['total'])}</strong> is due on <strong>{inv['due_date']}</strong>."
    else:
        from_name = settings['company_name'] or 'Us'
        subject = f"{type_label} #{inv['invoice_number']} from {from_name}"
        body_intro = f"Please find attached {type_label.lower()} <strong>#{inv['invoice_number']}</strong> for <strong>{format_naira(inv['total'])}</strong>."

    invoice_html = generate_invoice_html(invoice_id)

    email_body = f"""
    <html><body style="font-family:'Segoe UI',Arial,sans-serif;color:#1f2937;padding:20px;">
        <p>Dear {client['name']},</p>
        <p>{body_intro}</p>
        <p>Due Date: <strong>{inv['due_date']}</strong></p>
        <p>Please review the attached {type_label.lower()} and let us know if you have any questions.</p>
        <br>
        <p>Best regards,<br>{settings.get('smtp_from_name') or settings.get('company_name') or 'The Team'}</p>
    </body></html>
    """

    msg = MIMEMultipart()
    from_email = settings['smtp_from_email'] or settings['smtp_user']
    from_display = settings.get('smtp_from_name', '')
    msg["From"] = f"{from_display} <{from_email}>"
    msg["To"] = client["email"]
    msg["Subject"] = subject
    msg.attach(MIMEText(email_body, "html"))

    attachment = MIMEBase("text", "html")
    attachment.set_payload(invoice_html.encode())
    encoders.encode_base64(attachment)
    attachment.add_header("Content-Disposition", f"attachment; filename={inv['invoice_number']}.html")
    msg.attach(attachment)

    try:
        if settings["smtp_port"] == 465:
            server = smtplib.SMTP_SSL(settings["smtp_host"], settings["smtp_port"])
        else:
            server = smtplib.SMTP(settings["smtp_host"], settings["smtp_port"])
            server.starttls()
        server.login(settings["smtp_user"], settings["smtp_password"])
        server.send_message(msg)
        server.quit()

        conn = get_db()
        conn.execute(
            "INSERT INTO email_log (id, invoice_id, recipient, subject, type) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), invoice_id, client["email"], subject, "reminder" if is_reminder else "invoice")
        )
        if not is_reminder:
            conn.execute("UPDATE invoices SET status = 'sent', updated_at = datetime('now') WHERE id = ? AND status = 'draft'", (invoice_id,))
        else:
            conn.execute(
                "UPDATE invoices SET reminder_sent_count = reminder_sent_count + 1, last_reminder_sent = datetime('now'), updated_at = datetime('now') WHERE id = ?",
                (invoice_id,)
            )
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


class ReminderScheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)

    def run(self):
        while True:
            try:
                conn = get_db()
                invoices = [dict(r) for r in conn.execute("""
                    SELECT * FROM invoices
                    WHERE reminder_enabled = 1
                    AND status IN ('sent', 'overdue')
                    AND reminder_sent_count < reminder_max_count
                """).fetchall()]
                conn.close()

                for inv in invoices:
                    now = datetime.now()
                    if inv["last_reminder_sent"]:
                        last_sent = datetime.fromisoformat(inv["last_reminder_sent"])
                        next_reminder = last_sent + timedelta(days=inv["reminder_frequency_days"])
                    else:
                        due = datetime.fromisoformat(inv["due_date"])
                        next_reminder = due

                    if now >= next_reminder:
                        send_invoice_email(inv["id"], is_reminder=True)

                    due_date = datetime.fromisoformat(inv["due_date"])
                    if now > due_date and inv["status"] == "sent":
                        conn2 = get_db()
                        conn2.execute("UPDATE invoices SET status = 'overdue', updated_at = datetime('now') WHERE id = ?", (inv["id"],))
                        conn2.commit()
                        conn2.close()

            except Exception as e:
                print(f"Reminder error: {e}")

            time.sleep(3600)


class InvoiceHandler(http.server.BaseHTTPRequestHandler):

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def _send_html(self, html, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def _serve_static(self, path):
        clean = path[len("/static/"):] if path.startswith("/static/") else path.lstrip("/")
        file_path = os.path.join(STATIC_DIR, clean)
        if os.path.isfile(file_path):
            mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.end_headers()
            with open(file_path, "rb") as f:
                self.wfile.write(f.read())
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_upload(self, path):
        filename = path.split("/uploads/")[-1]
        file_path = os.path.join(UPLOAD_DIR, filename)
        if os.path.isfile(file_path):
            mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.end_headers()
            with open(file_path, "rb") as f:
                self.wfile.write(f.read())
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/" or path == "":
            self._serve_static("index.html")
        elif path.startswith("/static/"):
            self._serve_static(path)
        elif path.startswith("/uploads/"):
            self._serve_upload(path)
        elif path == "/api/settings":
            conn = get_db()
            settings = dict(conn.execute("SELECT * FROM settings WHERE id = 1").fetchone())
            conn.close()
            settings.pop("smtp_password", None)
            self._send_json(settings)
        elif path == "/api/clients":
            conn = get_db()
            clients = [dict(r) for r in conn.execute("SELECT * FROM clients ORDER BY name").fetchall()]
            conn.close()
            self._send_json(clients)
        elif path.startswith("/api/clients/"):
            cid = path.split("/")[-1]
            conn = get_db()
            client = conn.execute("SELECT * FROM clients WHERE id = ?", (cid,)).fetchone()
            conn.close()
            self._send_json(dict(client) if client else {}, 200 if client else 404)
        elif path == "/api/invoices":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            conn = get_db()
            query = "SELECT i.*, c.name as client_name, c.email as client_email FROM invoices i JOIN clients c ON i.client_id = c.id"
            params = []
            conditions = []
            if "type" in qs:
                conditions.append("i.type = ?")
                params.append(qs["type"][0])
            if "status" in qs:
                conditions.append("i.status = ?")
                params.append(qs["status"][0])
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY i.created_at DESC"
            invoices = [dict(r) for r in conn.execute(query, params).fetchall()]
            conn.close()
            self._send_json(invoices)
        elif path.startswith("/api/invoices/") and path.endswith("/preview"):
            inv_id = path.split("/")[-2]
            try:
                html = generate_invoice_html(inv_id)
                self._send_html(html)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif path.startswith("/api/invoices/") and path.endswith("/items"):
            inv_id = path.split("/")[-2]
            conn = get_db()
            items = [dict(r) for r in conn.execute(
                "SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY sort_order", (inv_id,)
            ).fetchall()]
            conn.close()
            self._send_json(items)
        elif path.startswith("/api/invoices/") and path.endswith("/emails"):
            inv_id = path.split("/")[-2]
            conn = get_db()
            emails = [dict(r) for r in conn.execute(
                "SELECT * FROM email_log WHERE invoice_id = ? ORDER BY sent_at DESC", (inv_id,)
            ).fetchall()]
            conn.close()
            self._send_json(emails)
        elif path.startswith("/api/invoices/"):
            inv_id = path.split("/")[-1]
            conn = get_db()
            inv = conn.execute(
                "SELECT i.*, c.name as client_name, c.email as client_email FROM invoices i JOIN clients c ON i.client_id = c.id WHERE i.id = ?",
                (inv_id,)
            ).fetchone()
            items = [dict(r) for r in conn.execute(
                "SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY sort_order", (inv_id,)
            ).fetchall()]
            conn.close()
            if inv:
                data = dict(inv)
                data["items"] = items
                self._send_json(data)
            else:
                self._send_json({"error": "Not found"}, 404)
        elif path == "/api/dashboard":
            conn = get_db()
            total_invoices = conn.execute("SELECT COUNT(*) as c FROM invoices WHERE type='invoice'").fetchone()["c"]
            total_quotes = conn.execute("SELECT COUNT(*) as c FROM invoices WHERE type='quote'").fetchone()["c"]
            paid = conn.execute("SELECT COALESCE(SUM(total),0) as s FROM invoices WHERE type='invoice' AND status='paid'").fetchone()["s"]
            outstanding = conn.execute("SELECT COALESCE(SUM(total),0) as s FROM invoices WHERE type='invoice' AND status IN ('sent','overdue')").fetchone()["s"]
            overdue = conn.execute("SELECT COUNT(*) as c FROM invoices WHERE status='overdue'").fetchone()["c"]
            recent = [dict(r) for r in conn.execute(
                "SELECT i.*, c.name as client_name FROM invoices i JOIN clients c ON i.client_id = c.id ORDER BY i.created_at DESC LIMIT 5"
            ).fetchall()]
            conn.close()
            self._send_json({
                "total_invoices": total_invoices,
                "total_quotes": total_quotes,
                "total_paid": paid,
                "total_outstanding": outstanding,
                "overdue_count": overdue,
                "recent": recent,
            })
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/settings":
            data = self._read_body()
            conn = get_db()
            fields = ["company_name", "company_address", "company_email", "company_phone",
                       "bank_name", "account_name", "account_number",
                       "smtp_host", "smtp_port", "smtp_user", "smtp_password",
                       "smtp_from_name", "smtp_from_email", "default_reminder_days"]
            sets = []
            vals = []
            for f in fields:
                if f in data:
                    sets.append(f"{f} = ?")
                    vals.append(data[f])
            if sets:
                conn.execute(f"UPDATE settings SET {', '.join(sets)} WHERE id = 1", vals)
                conn.commit()
            conn.close()
            self._send_json({"success": True})

        elif path == "/api/settings/logo":
            content_type = self.headers.get("Content-Type", "")
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)

            if "multipart/form-data" in content_type:
                boundary = content_type.split("boundary=")[1].encode()
                parts = body.split(b"--" + boundary)
                for part in parts:
                    if b"filename=" in part:
                        header_end = part.index(b"\r\n\r\n") + 4
                        file_data = part[header_end:].rstrip(b"\r\n--")
                        filename_start = part.index(b'filename="') + 10
                        filename_end = part.index(b'"', filename_start)
                        original_name = part[filename_start:filename_end].decode()
                        ext = os.path.splitext(original_name)[1] or ".png"
                        new_name = f"logo_{uuid.uuid4().hex[:8]}{ext}"
                        filepath = os.path.join(UPLOAD_DIR, new_name)
                        with open(filepath, "wb") as f:
                            f.write(file_data)
                        conn = get_db()
                        conn.execute("UPDATE settings SET company_logo = ? WHERE id = 1", (new_name,))
                        conn.commit()
                        conn.close()
                        self._send_json({"success": True, "filename": new_name})
                        return
            self._send_json({"error": "No file uploaded"}, 400)

        elif path == "/api/clients":
            data = self._read_body()
            cid = str(uuid.uuid4())
            conn = get_db()
            conn.execute(
                "INSERT INTO clients (id, name, email, phone, address, company) VALUES (?, ?, ?, ?, ?, ?)",
                (cid, data["name"], data["email"], data.get("phone", ""), data.get("address", ""), data.get("company", ""))
            )
            conn.commit()
            conn.close()
            self._send_json({"success": True, "id": cid})

        elif path == "/api/invoices":
            data = self._read_body()
            inv_id = str(uuid.uuid4())
            inv_number = generate_invoice_number(data["type"])
            items = data.get("items", [])
            totals = calculate_totals(items, data.get("discount_percent", 0))

            conn = get_db()
            conn.execute("""
                INSERT INTO invoices (id, invoice_number, type, status, client_id, issue_date, due_date,
                    subtotal, vat_amount, discount_percent, discount_amount, total, notes, terms,
                    reminder_enabled, reminder_frequency_days, reminder_max_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                inv_id, inv_number, data["type"], data.get("status", "draft"),
                data["client_id"], data["issue_date"], data["due_date"],
                totals["subtotal"], totals["vat_amount"],
                data.get("discount_percent", 0), totals["discount_amount"], totals["total"],
                data.get("notes", ""), data.get("terms", ""),
                1 if data.get("reminder_enabled") else 0,
                data.get("reminder_frequency_days", 7),
                data.get("reminder_max_count", 3),
            ))

            for i, item in enumerate(items):
                conn.execute(
                    "INSERT INTO invoice_items (id, invoice_id, description, quantity, unit_price, amount, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), inv_id, item["description"], item["quantity"], item["unit_price"],
                     round(item["quantity"] * item["unit_price"], 2), i)
                )
            conn.commit()
            conn.close()
            self._send_json({"success": True, "id": inv_id, "invoice_number": inv_number})

        elif path.startswith("/api/invoices/") and path.endswith("/send"):
            inv_id = path.split("/")[-2]
            result = send_invoice_email(inv_id)
            self._send_json(result, 200 if result["success"] else 500)

        elif path.startswith("/api/invoices/") and path.endswith("/remind"):
            inv_id = path.split("/")[-2]
            result = send_invoice_email(inv_id, is_reminder=True)
            self._send_json(result, 200 if result["success"] else 500)

        else:
            self._send_json({"error": "Not found"}, 404)

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path

        if path.startswith("/api/clients/"):
            cid = path.split("/")[-1]
            data = self._read_body()
            conn = get_db()
            conn.execute(
                "UPDATE clients SET name=?, email=?, phone=?, address=?, company=? WHERE id=?",
                (data["name"], data["email"], data.get("phone", ""), data.get("address", ""), data.get("company", ""), cid)
            )
            conn.commit()
            conn.close()
            self._send_json({"success": True})

        elif path.startswith("/api/invoices/") and path.endswith("/status"):
            inv_id = path.split("/")[-2]
            data = self._read_body()
            conn = get_db()
            conn.execute("UPDATE invoices SET status = ?, updated_at = datetime('now') WHERE id = ?", (data["status"], inv_id))
            conn.commit()
            conn.close()
            self._send_json({"success": True})

        elif path.startswith("/api/invoices/"):
            inv_id = path.split("/")[-1]
            data = self._read_body()
            items = data.get("items", [])
            totals = calculate_totals(items, data.get("discount_percent", 0))

            conn = get_db()
            conn.execute("""
                UPDATE invoices SET client_id=?, issue_date=?, due_date=?,
                    subtotal=?, vat_amount=?, discount_percent=?, discount_amount=?, total=?,
                    notes=?, terms=?, reminder_enabled=?, reminder_frequency_days=?,
                    reminder_max_count=?, updated_at=datetime('now')
                WHERE id=?
            """, (
                data["client_id"], data["issue_date"], data["due_date"],
                totals["subtotal"], totals["vat_amount"],
                data.get("discount_percent", 0), totals["discount_amount"], totals["total"],
                data.get("notes", ""), data.get("terms", ""),
                1 if data.get("reminder_enabled") else 0,
                data.get("reminder_frequency_days", 7),
                data.get("reminder_max_count", 3),
                inv_id,
            ))
            conn.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (inv_id,))
            for i, item in enumerate(items):
                conn.execute(
                    "INSERT INTO invoice_items (id, invoice_id, description, quantity, unit_price, amount, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), inv_id, item["description"], item["quantity"], item["unit_price"],
                     round(item["quantity"] * item["unit_price"], 2), i)
                )
            conn.commit()
            conn.close()
            self._send_json({"success": True})

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/clients/"):
            cid = path.split("/")[-1]
            conn = get_db()
            conn.execute("DELETE FROM clients WHERE id = ?", (cid,))
            conn.commit()
            conn.close()
            self._send_json({"success": True})
        elif path.startswith("/api/invoices/"):
            inv_id = path.split("/")[-1]
            conn = get_db()
            conn.execute("DELETE FROM invoices WHERE id = ?", (inv_id,))
            conn.commit()
            conn.close()
            self._send_json({"success": True})

    def log_message(self, format, *args):
        pass  # Suppress default logging


def main():
    init_db()
    scheduler = ReminderScheduler()
    scheduler.start()
    server = http.server.HTTPServer(("0.0.0.0", PORT), InvoiceHandler)
    print(f"""
╔══════════════════════════════════════════════════╗
║           🧾 Naija Invoice is running!           ║
║                                                  ║
║   Open your browser and go to:                   ║
║   👉  http://localhost:{PORT}                      ║
║                                                  ║
║   Press Ctrl+C to stop the server                ║
╚══════════════════════════════════════════════════╝
    """)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
