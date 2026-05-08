#!/usr/bin/env python3
"""FlowLedger — Nigerian Business Invoicing. Run: python3 server.py"""

import http.server, json, sqlite3, os, uuid, base64, mimetypes
import urllib.parse, smtplib, threading, time, hashlib, secrets, http.cookies
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

PORT = int(os.environ.get("PORT", 8080))
# On Render (and similar), use /data for persistent storage; otherwise use local dir
_DATA_DIR = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(_DATA_DIR, "flowledger.db")
UPLOAD_DIR = os.path.join(_DATA_DIR, "uploads")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

VAT_RATE = 7.5
SESSION_COOKIE = "fl_session"
SESSION_MAX_AGE = 30 * 24 * 3600

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL, company_name TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')), expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL UNIQUE,
    company_name TEXT DEFAULT '', company_address TEXT DEFAULT '',
    company_email TEXT DEFAULT '', company_phone TEXT DEFAULT '',
    company_logo TEXT DEFAULT '', tax_number TEXT DEFAULT '',
    bank_name TEXT DEFAULT '', account_name TEXT DEFAULT '', account_number TEXT DEFAULT '',
    smtp_host TEXT DEFAULT '', smtp_port INTEGER DEFAULT 587,
    smtp_user TEXT DEFAULT '', smtp_password TEXT DEFAULT '',
    smtp_from_name TEXT DEFAULT '', smtp_from_email TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS clients (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
    email TEXT NOT NULL, phone TEXT DEFAULT '', address TEXT DEFAULT '',
    company TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, invoice_number TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('invoice','quote')),
    status TEXT DEFAULT 'draft', client_id TEXT NOT NULL,
    issue_date TEXT NOT NULL, due_date TEXT NOT NULL,
    subtotal REAL DEFAULT 0, vat_amount REAL DEFAULT 0,
    discount_percent REAL DEFAULT 0, discount_amount REAL DEFAULT 0,
    total REAL DEFAULT 0, notes TEXT DEFAULT '', terms TEXT DEFAULT '',
    currency TEXT DEFAULT 'NGN', payment_method TEXT DEFAULT '',
    payment_date TEXT DEFAULT '', reminder_enabled INTEGER DEFAULT 0,
    reminder_frequency_days INTEGER DEFAULT 7, reminder_max_count INTEGER DEFAULT 3,
    reminder_sent_count INTEGER DEFAULT 0, last_reminder_sent TEXT,
    created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS invoice_items (
    id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL, description TEXT NOT NULL,
    quantity REAL DEFAULT 1, unit_price REAL DEFAULT 0,
    amount REAL DEFAULT 0, sort_order INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS email_log (
    id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL, recipient TEXT NOT NULL,
    subject TEXT NOT NULL, type TEXT DEFAULT 'invoice',
    status TEXT DEFAULT 'sent', sent_at TEXT DEFAULT (datetime('now'))
);
"""


# ── Auth helpers ──────────────────────────────────────────────────────────────

def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return salt + ":" + h.hex()

def verify_password(password, stored):
    try:
        salt = stored.split(":")[0]
        return hash_password(password, salt) == stored
    except Exception:
        return False


# ── DB init & migration ───────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def _column_exists(conn, table, col):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == col for r in rows)

def _apply_column_migrations(conn):
    if not _column_exists(conn, "settings", "tax_number"):
        conn.execute("ALTER TABLE settings ADD COLUMN tax_number TEXT DEFAULT ''")
    conn.commit()

def _restore_from_v1(conn, saved):
    old_s = (saved.get("settings") or [{}])[0]
    uid = str(uuid.uuid4())
    email = (old_s.get("company_email") or "").strip() or "owner@flowledger.local"
    company = (old_s.get("company_name") or "").strip() or "My Company"
    conn.execute(
        "INSERT INTO users (id,name,email,password_hash,company_name) VALUES (?,?,?,?,?)",
        (uid, "Account Owner", email, hash_password("flowledger123"), company)
    )
    if old_s:
        conn.execute("""INSERT INTO settings
            (user_id,company_name,company_address,company_email,company_phone,
             company_logo,bank_name,account_name,account_number,
             smtp_host,smtp_port,smtp_user,smtp_password,smtp_from_name,smtp_from_email)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (uid, old_s.get("company_name",""), old_s.get("company_address",""),
             old_s.get("company_email",""), old_s.get("company_phone",""),
             old_s.get("company_logo",""), old_s.get("bank_name",""),
             old_s.get("account_name",""), old_s.get("account_number",""),
             old_s.get("smtp_host",""), old_s.get("smtp_port",587),
             old_s.get("smtp_user",""), old_s.get("smtp_password",""),
             old_s.get("smtp_from_name",""), old_s.get("smtp_from_email",""))
        )
    else:
        conn.execute("INSERT INTO settings (user_id) VALUES (?)", (uid,))
    for c in saved.get("clients", []):
        conn.execute("INSERT INTO clients (id,user_id,name,email,phone,address,company,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (c["id"], uid, c["name"], c["email"], c.get("phone",""), c.get("address",""), c.get("company",""), c.get("created_at","")))
    for inv in saved.get("invoices", []):
        conn.execute("""INSERT INTO invoices
            (id,user_id,invoice_number,type,status,client_id,issue_date,due_date,
             subtotal,vat_amount,discount_percent,discount_amount,total,notes,terms,
             currency,payment_method,payment_date,reminder_enabled,reminder_frequency_days,
             reminder_max_count,reminder_sent_count,last_reminder_sent,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (inv["id"], uid, inv["invoice_number"], inv["type"], inv["status"],
             inv["client_id"], inv["issue_date"], inv["due_date"],
             inv.get("subtotal",0), inv.get("vat_amount",0), inv.get("discount_percent",0),
             inv.get("discount_amount",0), inv.get("total",0), inv.get("notes",""),
             inv.get("terms",""), inv.get("currency","NGN"), inv.get("payment_method",""),
             inv.get("payment_date",""), inv.get("reminder_enabled",0),
             inv.get("reminder_frequency_days",7), inv.get("reminder_max_count",3),
             inv.get("reminder_sent_count",0), inv.get("last_reminder_sent"),
             inv.get("created_at",""), inv.get("updated_at","")))
    for item in saved.get("invoice_items", []):
        conn.execute("INSERT INTO invoice_items (id,invoice_id,description,quantity,unit_price,amount,sort_order) VALUES (?,?,?,?,?,?,?)",
            (item["id"], item["invoice_id"], item["description"], item["quantity"], item["unit_price"], item["amount"], item.get("sort_order",0)))
    for e in saved.get("email_log", []):
        conn.execute("INSERT INTO email_log (id,invoice_id,recipient,subject,type,status,sent_at) VALUES (?,?,?,?,?,?,?)",
            (e["id"], e["invoice_id"], e["recipient"], e["subject"], e.get("type","invoice"), e.get("status","sent"), e.get("sent_at","")))
    conn.commit()
    print(f"\n{'='*54}")
    print("  DATABASE MIGRATED TO MULTI-USER SCHEMA")
    print(f"  Sign-in email   : {email}")
    print("  Sign-in password: flowledger123")
    print("  Change your password in Settings → Account.")
    print('='*54 + "\n")

def init_db():
    conn = get_db()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    old_schema = "clients" in tables and "users" not in tables
    saved = {}
    if old_schema:
        for t in ["settings","clients","invoices","invoice_items","email_log"]:
            if t in tables:
                try: saved[t] = [dict(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()]
                except: saved[t] = []
        conn.execute("PRAGMA foreign_keys=OFF")
        for t in ["email_log","invoice_items","invoices","clients","settings"]:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()
    conn.executescript(SCHEMA_SQL)
    if old_schema:
        _restore_from_v1(conn, saved)
    elif "users" in tables:
        _apply_column_migrations(conn)
    conn.close()


# ── Invoice helpers ───────────────────────────────────────────────────────────

def generate_invoice_number(inv_type, user_id):
    conn = get_db()
    prefix = "INV" if inv_type == "invoice" else "QT"
    year = datetime.now().strftime("%Y")
    cnt = conn.execute(
        "SELECT COUNT(*) as c FROM invoices WHERE user_id=? AND type=? AND invoice_number LIKE ?",
        (user_id, inv_type, f"{prefix}-{year}-%")
    ).fetchone()["c"]
    conn.close()
    return f"{prefix}-{year}-{cnt+1:04d}"

def calculate_totals(items, discount_percent=0):
    subtotal = sum(i["quantity"] * i["unit_price"] for i in items)
    disc = subtotal * (discount_percent / 100)
    taxable = subtotal - disc
    vat = taxable * (VAT_RATE / 100)
    return {
        "subtotal": round(subtotal, 2), "discount_amount": round(disc, 2),
        "vat_amount": round(vat, 2), "total": round(taxable + vat, 2),
    }

def fmt_amount(amount, currency="NGN"):
    sym = "$" if currency == "USD" else "₦"
    return f"{sym}{amount:,.2f}"

def generate_invoice_html(invoice_id):
    conn = get_db()
    inv = dict(conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone())
    client = dict(conn.execute("SELECT * FROM clients WHERE id=?", (inv["client_id"],)).fetchone())
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY sort_order", (invoice_id,)
    ).fetchall()]
    srow = conn.execute("SELECT * FROM settings WHERE user_id=?", (inv["user_id"],)).fetchone()
    s = dict(srow) if srow else {}
    conn.close()

    currency = inv.get("currency", "NGN")
    def fmt(a): return fmt_amount(a, currency)

    logo_html = ""
    if s.get("company_logo"):
        lp = os.path.join(UPLOAD_DIR, s["company_logo"])
        if os.path.exists(lp):
            mime = mimetypes.guess_type(lp)[0] or "image/png"
            with open(lp, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            logo_html = f'<img src="data:{mime};base64,{b64}" style="max-height:80px;max-width:200px;" />'

    status_color = {"draft":"#6b7280","sent":"#2563eb","paid":"#16a34a",
        "overdue":"#dc2626","cancelled":"#9ca3af","accepted":"#16a34a","declined":"#dc2626"
    }.get(inv["status"], "#6b7280")
    type_label = "INVOICE" if inv["type"] == "invoice" else "QUOTATION"

    items_html = "".join(f"""
        <tr>
            <td style="padding:10px;border-bottom:1px solid #e5e7eb;">{i+1}</td>
            <td style="padding:10px;border-bottom:1px solid #e5e7eb;">{item['description']}</td>
            <td style="padding:10px;border-bottom:1px solid #e5e7eb;text-align:center;">{item['quantity']}</td>
            <td style="padding:10px;border-bottom:1px solid #e5e7eb;text-align:right;">{fmt(item['unit_price'])}</td>
            <td style="padding:10px;border-bottom:1px solid #e5e7eb;text-align:right;">{fmt(item['amount'])}</td>
        </tr>""" for i, item in enumerate(items))

    discount_row = ""
    if inv["discount_percent"] > 0:
        discount_row = f'<tr><td style="padding:8px 10px;text-align:right;color:#6b7280;">Discount ({inv["discount_percent"]}%)</td><td style="padding:8px 10px;text-align:right;color:#dc2626;">-{fmt(inv["discount_amount"])}</td></tr>'

    tax_line = f'<p style="margin:2px 0;font-size:13px;color:#6b7280;">TIN: {s["tax_number"]}</p>' if s.get("tax_number") else ""
    company_name = s.get("company_name") or "Your Company"

    notes_html = ""
    if inv["notes"]:
        notes_html = f'<div style="margin-top:20px;padding:15px;background:#f9fafb;border-radius:8px;"><p style="margin:0 0 5px;font-size:12px;color:#9ca3af;text-transform:uppercase;">Notes</p><p style="margin:0;font-size:13px;">{inv["notes"]}</p></div>'

    terms_html = ""
    if inv["terms"]:
        terms_html = f'<div style="margin-top:15px;padding:15px;background:#f9fafb;border-radius:8px;"><p style="margin:0 0 5px;font-size:12px;color:#9ca3af;text-transform:uppercase;">Terms &amp; Conditions</p><p style="margin:0;font-size:13px;">{inv["terms"]}</p></div>'

    bank_html = ""
    if s.get("bank_name") or s.get("account_number"):
        bank_html = f'''<div style="margin-top:20px;padding:15px;background:#f0fdf4;border-radius:8px;border:1px solid #bbf7d0;">
            <h3 style="margin:0 0 10px;color:#16a34a;font-size:14px;">Payment Instructions</h3>
            <p style="margin:3px 0;font-size:13px;"><strong>Bank:</strong> {s.get("bank_name","")}</p>
            <p style="margin:3px 0;font-size:13px;"><strong>Account Name:</strong> {s.get("account_name","")}</p>
            <p style="margin:3px 0;font-size:13px;"><strong>Account Number:</strong> {s.get("account_number","")}</p>
        </div>'''

    paid_html = ""
    if inv["status"] == "paid" and inv.get("payment_method"):
        labels = {"cash":"Cash","bank_transfer":"Bank Transfer","cheque":"Cheque"}
        ml = labels.get(inv["payment_method"], inv["payment_method"])
        date_line = f'<p style="margin:3px 0;font-size:13px;"><strong>Payment Date:</strong> {inv["payment_date"]}</p>' if inv.get("payment_date") else ""
        paid_html = f'<div style="margin-top:15px;padding:15px;background:#f0fdf4;border-radius:8px;border:1px solid #bbf7d0;"><p style="margin:0;font-size:13px;"><strong>Payment Method:</strong> {ml}</p>{date_line}</div>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{type_label} {inv['invoice_number']}</title></head>
<body style="font-family:'Segoe UI',Arial,sans-serif;max-width:800px;margin:0 auto;padding:40px;color:#1f2937;">
<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:40px;">
    <div>
        {logo_html}
        <h2 style="margin:10px 0 5px;color:#111827;">{company_name}</h2>
        <p style="margin:2px 0;font-size:13px;color:#6b7280;">{s.get('company_address','')}</p>
        <p style="margin:2px 0;font-size:13px;color:#6b7280;">{s.get('company_email','')}</p>
        <p style="margin:2px 0;font-size:13px;color:#6b7280;">{s.get('company_phone','')}</p>
        {tax_line}
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
    <thead><tr style="background:#f9fafb;">
        <th style="padding:10px;text-align:left;font-size:12px;color:#6b7280;text-transform:uppercase;border-bottom:2px solid #e5e7eb;width:40px;">#</th>
        <th style="padding:10px;text-align:left;font-size:12px;color:#6b7280;text-transform:uppercase;border-bottom:2px solid #e5e7eb;">Description</th>
        <th style="padding:10px;text-align:center;font-size:12px;color:#6b7280;text-transform:uppercase;border-bottom:2px solid #e5e7eb;width:80px;">Qty</th>
        <th style="padding:10px;text-align:right;font-size:12px;color:#6b7280;text-transform:uppercase;border-bottom:2px solid #e5e7eb;width:130px;">Unit Price</th>
        <th style="padding:10px;text-align:right;font-size:12px;color:#6b7280;text-transform:uppercase;border-bottom:2px solid #e5e7eb;width:130px;">Amount</th>
    </tr></thead>
    <tbody>{items_html}</tbody>
</table>
<div style="display:flex;justify-content:flex-end;">
    <table style="min-width:280px;">
        <tr><td style="padding:8px 10px;text-align:right;color:#6b7280;">Subtotal</td><td style="padding:8px 10px;text-align:right;font-weight:500;">{fmt(inv['subtotal'])}</td></tr>
        {discount_row}
        <tr><td style="padding:8px 10px;text-align:right;color:#6b7280;">VAT ({VAT_RATE}%)</td><td style="padding:8px 10px;text-align:right;font-weight:500;">{fmt(inv['vat_amount'])}</td></tr>
        <tr style="border-top:2px solid #111827;"><td style="padding:12px 10px;text-align:right;font-weight:700;font-size:16px;">Total</td><td style="padding:12px 10px;text-align:right;font-weight:700;font-size:16px;color:#111827;">{fmt(inv['total'])}</td></tr>
    </table>
</div>
{notes_html}
{terms_html}
{bank_html}
{paid_html}
<div style="margin-top:40px;text-align:center;color:#9ca3af;font-size:11px;">
    <p>Generated by FlowLedger | VAT compliant as per Nigerian tax law (FIRS)</p>
</div>
</body></html>"""


def send_invoice_email(invoice_id, is_reminder=False):
    conn = get_db()
    inv = dict(conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone())
    client = dict(conn.execute("SELECT * FROM clients WHERE id=?", (inv["client_id"],)).fetchone())
    srow = conn.execute("SELECT * FROM settings WHERE user_id=?", (inv["user_id"],)).fetchone()
    s = dict(srow) if srow else {}
    conn.close()

    if not s.get("smtp_host") or not s.get("smtp_user"):
        return {"success": False, "error": "Email not configured. Go to Settings → Email to set up SMTP."}

    currency = inv.get("currency", "NGN")
    def fmt(a): return fmt_amount(a, currency)

    type_label = "Invoice" if inv["type"] == "invoice" else "Quotation"
    if is_reminder:
        subject = f"Reminder: {type_label} #{inv['invoice_number']} - Payment Due"
        body_intro = f"This is a friendly reminder that {type_label.lower()} <strong>#{inv['invoice_number']}</strong> for <strong>{fmt(inv['total'])}</strong> is due on <strong>{inv['due_date']}</strong>."
    else:
        subject = f"{type_label} #{inv['invoice_number']} from {s.get('company_name','Us')}"
        body_intro = f"Please find attached {type_label.lower()} <strong>#{inv['invoice_number']}</strong> for <strong>{fmt(inv['total'])}</strong>."

    invoice_html = generate_invoice_html(invoice_id)
    email_body = f"""<html><body style="font-family:'Segoe UI',Arial,sans-serif;color:#1f2937;padding:20px;">
        <p>Dear {client['name']},</p><p>{body_intro}</p>
        <p>Due Date: <strong>{inv['due_date']}</strong></p>
        <p>Please review the attached {type_label.lower()} and contact us with any questions.</p>
        <br><p>Best regards,<br>{s.get('smtp_from_name') or s.get('company_name') or 'The Team'}</p>
    </body></html>"""

    msg = MIMEMultipart()
    from_email = s.get("smtp_from_email") or s.get("smtp_user","")
    msg["From"] = f"{s.get('smtp_from_name','')} <{from_email}>"
    msg["To"] = client["email"]
    msg["Subject"] = subject
    msg.attach(MIMEText(email_body, "html"))
    att = MIMEBase("text", "html")
    att.set_payload(invoice_html.encode())
    encoders.encode_base64(att)
    att.add_header("Content-Disposition", f"attachment; filename={inv['invoice_number']}.html")
    msg.attach(att)

    try:
        if s.get("smtp_port") == 465:
            srv = smtplib.SMTP_SSL(s["smtp_host"], s["smtp_port"])
        else:
            srv = smtplib.SMTP(s["smtp_host"], s["smtp_port"])
            srv.starttls()
        srv.login(s["smtp_user"], s["smtp_password"])
        srv.send_message(msg); srv.quit()

        conn = get_db()
        conn.execute("INSERT INTO email_log (id,invoice_id,recipient,subject,type) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), invoice_id, client["email"], subject, "reminder" if is_reminder else "invoice"))
        if not is_reminder:
            conn.execute("UPDATE invoices SET status='sent',updated_at=datetime('now') WHERE id=? AND status='draft'", (invoice_id,))
        else:
            conn.execute("UPDATE invoices SET reminder_sent_count=reminder_sent_count+1,last_reminder_sent=datetime('now'),updated_at=datetime('now') WHERE id=?", (invoice_id,))
        conn.commit(); conn.close()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


class ReminderScheduler(threading.Thread):
    def __init__(self): super().__init__(daemon=True)
    def run(self):
        while True:
            try:
                conn = get_db()
                invs = [dict(r) for r in conn.execute(
                    "SELECT * FROM invoices WHERE reminder_enabled=1 AND status IN ('sent','overdue') AND reminder_sent_count<reminder_max_count"
                ).fetchall()]
                conn.close()
                for inv in invs:
                    now = datetime.now()
                    if inv["last_reminder_sent"]:
                        nxt = datetime.fromisoformat(inv["last_reminder_sent"]) + timedelta(days=inv["reminder_frequency_days"])
                    else:
                        nxt = datetime.fromisoformat(inv["due_date"])
                    if now >= nxt:
                        send_invoice_email(inv["id"], is_reminder=True)
                    if now > datetime.fromisoformat(inv["due_date"]) and inv["status"] == "sent":
                        c2 = get_db()
                        c2.execute("UPDATE invoices SET status='overdue',updated_at=datetime('now') WHERE id=?", (inv["id"],))
                        c2.commit(); c2.close()
            except Exception as e:
                print(f"Reminder error: {e}")
            time.sleep(3600)


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class InvoiceHandler(http.server.BaseHTTPRequestHandler):

    def _session_user(self):
        cookies = http.cookies.SimpleCookie(self.headers.get("Cookie",""))
        if SESSION_COOKIE not in cookies: return None
        sid = cookies[SESSION_COOKIE].value
        conn = get_db()
        row = conn.execute("SELECT user_id FROM sessions WHERE id=? AND expires_at>datetime('now')", (sid,)).fetchone()
        conn.close()
        return row["user_id"] if row else None

    def _require_auth(self):
        uid = self._session_user()
        if not uid: self._send_json({"error":"Not authenticated"}, 401)
        return uid

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def _send_json_cookie(self, data, sid, status=200):
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        c = http.cookies.SimpleCookie()
        c[SESSION_COOKIE] = sid
        c[SESSION_COOKIE]["path"] = "/"
        c[SESSION_COOKIE]["max-age"] = str(SESSION_MAX_AGE)
        c[SESSION_COOKIE]["httponly"] = True
        c[SESSION_COOKIE]["samesite"] = "Lax"
        self.send_header("Set-Cookie", c[SESSION_COOKIE].OutputString())
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def _send_json_clear_cookie(self, data):
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        c = http.cookies.SimpleCookie()
        c[SESSION_COOKIE] = ""; c[SESSION_COOKIE]["path"] = "/"; c[SESSION_COOKIE]["max-age"] = "0"
        self.send_header("Set-Cookie", c[SESSION_COOKIE].OutputString())
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _send_html(self, html, status=200):
        self.send_response(status)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _body(self):
        n = int(self.headers.get("Content-Length",0))
        return json.loads(self.rfile.read(n)) if n else {}

    def _static(self, path):
        clean = path[len("/static/"):] if path.startswith("/static/") else path.lstrip("/")
        fp = os.path.join(STATIC_DIR, clean)
        if os.path.isfile(fp):
            mime = mimetypes.guess_type(fp)[0] or "application/octet-stream"
            self.send_response(200); self.send_header("Content-Type", mime); self.end_headers()
            with open(fp,"rb") as f: self.wfile.write(f.read())
        else:
            self.send_response(404); self.end_headers()

    def _upload(self, path):
        fn = path.split("/uploads/")[-1]
        fp = os.path.join(UPLOAD_DIR, fn)
        if os.path.isfile(fp):
            mime = mimetypes.guess_type(fp)[0] or "application/octet-stream"
            self.send_response(200); self.send_header("Content-Type", mime); self.end_headers()
            with open(fp,"rb") as f: self.wfile.write(f.read())
        else:
            self.send_response(404); self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        for h,v in [("Access-Control-Allow-Origin","*"),("Access-Control-Allow-Methods","GET,POST,PUT,DELETE,OPTIONS"),("Access-Control-Allow-Headers","Content-Type")]:
            self.send_header(h,v)
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        if path in ("/",""):
            self._static("index.html")
        elif path.startswith("/static/"):
            self._static(path)
        elif path.startswith("/uploads/"):
            self._upload(path)

        elif path == "/api/auth/me":
            uid = self._session_user()
            if not uid: self._send_json({"authenticated":False}); return
            conn = get_db()
            u = conn.execute("SELECT id,name,email,company_name FROM users WHERE id=?", (uid,)).fetchone()
            conn.close()
            self._send_json({"authenticated":True,"user":dict(u)} if u else {"authenticated":False})

        elif path == "/api/settings":
            uid = self._require_auth()
            if not uid: return
            conn = get_db()
            row = conn.execute("SELECT * FROM settings WHERE user_id=?", (uid,)).fetchone()
            conn.close()
            if row:
                s = dict(row); s.pop("smtp_password",None); self._send_json(s)
            else:
                self._send_json({})

        elif path == "/api/clients":
            uid = self._require_auth()
            if not uid: return
            conn = get_db()
            rows = [dict(r) for r in conn.execute("SELECT * FROM clients WHERE user_id=? ORDER BY name", (uid,)).fetchall()]
            conn.close(); self._send_json(rows)

        elif path.startswith("/api/clients/"):
            uid = self._require_auth()
            if not uid: return
            cid = path.split("/")[-1]; conn = get_db()
            row = conn.execute("SELECT * FROM clients WHERE id=? AND user_id=?", (cid,uid)).fetchone()
            conn.close(); self._send_json(dict(row) if row else {}, 200 if row else 404)

        elif path == "/api/invoices":
            uid = self._require_auth()
            if not uid: return
            conn = get_db()
            q = "SELECT i.*,c.name as client_name,c.email as client_email FROM invoices i JOIN clients c ON i.client_id=c.id WHERE i.user_id=?"
            params = [uid]
            if "type" in qs: q += " AND i.type=?"; params.append(qs["type"][0])
            if "status" in qs: q += " AND i.status=?"; params.append(qs["status"][0])
            q += " ORDER BY i.created_at DESC"
            rows = [dict(r) for r in conn.execute(q, params).fetchall()]
            conn.close(); self._send_json(rows)

        elif path.startswith("/api/invoices/") and path.endswith("/preview"):
            inv_id = path.split("/")[-2]
            try: self._send_html(generate_invoice_html(inv_id))
            except Exception as e: self._send_json({"error":str(e)},500)

        elif path.startswith("/api/invoices/") and path.endswith("/emails"):
            uid = self._require_auth()
            if not uid: return
            inv_id = path.split("/")[-2]; conn = get_db()
            rows = [dict(r) for r in conn.execute("SELECT * FROM email_log WHERE invoice_id=? ORDER BY sent_at DESC", (inv_id,)).fetchall()]
            conn.close(); self._send_json(rows)

        elif path.startswith("/api/invoices/"):
            uid = self._require_auth()
            if not uid: return
            inv_id = path.split("/")[-1]; conn = get_db()
            inv = conn.execute("SELECT i.*,c.name as client_name FROM invoices i JOIN clients c ON i.client_id=c.id WHERE i.id=? AND i.user_id=?", (inv_id,uid)).fetchone()
            items = [dict(r) for r in conn.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY sort_order", (inv_id,)).fetchall()]
            conn.close()
            if inv:
                d = dict(inv); d["items"] = items; self._send_json(d)
            else: self._send_json({"error":"Not found"},404)

        elif path == "/api/dashboard":
            uid = self._require_auth()
            if not uid: return
            conn = get_db()
            def q1(sql, p): return conn.execute(sql, p).fetchone()[0]
            data = {
                "total_invoices": q1("SELECT COUNT(*) FROM invoices WHERE user_id=? AND type='invoice'", (uid,)),
                "total_quotes":   q1("SELECT COUNT(*) FROM invoices WHERE user_id=? AND type='quote'",   (uid,)),
                "total_paid":     q1("SELECT COALESCE(SUM(total),0) FROM invoices WHERE user_id=? AND type='invoice' AND status='paid'", (uid,)),
                "total_outstanding": q1("SELECT COALESCE(SUM(total),0) FROM invoices WHERE user_id=? AND type='invoice' AND status IN ('sent','overdue')", (uid,)),
                "overdue_count":  q1("SELECT COUNT(*) FROM invoices WHERE user_id=? AND status='overdue'", (uid,)),
                "recent": [dict(r) for r in conn.execute("SELECT i.*,c.name as client_name FROM invoices i JOIN clients c ON i.client_id=c.id WHERE i.user_id=? ORDER BY i.created_at DESC LIMIT 5", (uid,)).fetchall()],
            }
            conn.close(); self._send_json(data)

        elif path == "/api/ledger":
            uid = self._require_auth()
            if not uid: return
            conn = get_db()
            clients_rows = conn.execute("""
                SELECT c.id, c.name, c.company,
                    COALESCE(SUM(CASE WHEN i.type='invoice' THEN i.total ELSE 0 END),0) as total_invoiced,
                    COALESCE(SUM(CASE WHEN i.type='invoice' AND i.status='paid' THEN i.total ELSE 0 END),0) as total_paid,
                    COALESCE(SUM(CASE WHEN i.type='invoice' AND i.status IN ('sent','overdue') THEN i.total ELSE 0 END),0) as total_pending
                FROM clients c LEFT JOIN invoices i ON c.id=i.client_id
                WHERE c.user_id=? GROUP BY c.id ORDER BY total_invoiced DESC
            """, (uid,)).fetchall()
            result = []
            for row in clients_rows:
                cd = dict(row)
                cd["invoices"] = [dict(r) for r in conn.execute(
                    "SELECT id,invoice_number,status,total,issue_date,payment_method FROM invoices WHERE client_id=? AND type='invoice' AND user_id=? ORDER BY issue_date DESC",
                    (cd["id"], uid)
                ).fetchall()]
                result.append(cd)
            conn.close(); self._send_json(result)

        elif path == "/api/reports/pnl":
            uid = self._require_auth()
            if not uid: return
            s, e = qs.get("start_date",["2000-01-01"])[0], qs.get("end_date",["2099-12-31"])[0]
            conn = get_db()
            row = conn.execute("""SELECT COALESCE(SUM(total),0) as total_revenue,
                COALESCE(SUM(vat_amount),0) as total_vat, COALESCE(SUM(subtotal),0) as net_revenue,
                COALESCE(SUM(discount_amount),0) as total_discounts, COUNT(*) as invoice_count
                FROM invoices WHERE user_id=? AND type='invoice' AND issue_date BETWEEN ? AND ?""",
                (uid,s,e)).fetchone()
            d = dict(row); d["net_income"] = d["net_revenue"] - d["total_discounts"]
            conn.close(); self._send_json(d)

        elif path == "/api/reports/balance":
            uid = self._require_auth()
            if not uid: return
            s, e = qs.get("start_date",["2000-01-01"])[0], qs.get("end_date",["2099-12-31"])[0]
            conn = get_db()
            def qv(sql): return conn.execute(sql, (uid,s,e)).fetchone()[0]
            paid = qv("SELECT COALESCE(SUM(total),0) FROM invoices WHERE user_id=? AND type='invoice' AND status='paid' AND issue_date BETWEEN ? AND ?")
            ar   = qv("SELECT COALESCE(SUM(total),0) FROM invoices WHERE user_id=? AND type='invoice' AND status IN ('sent','overdue') AND issue_date BETWEEN ? AND ?")
            vat  = qv("SELECT COALESCE(SUM(vat_amount),0) FROM invoices WHERE user_id=? AND type='invoice' AND status='paid' AND issue_date BETWEEN ? AND ?")
            conn.close()
            self._send_json({"total_paid":paid,"accounts_receivable":ar,"total_assets":paid+ar,"vat_payable":vat,"equity":paid+ar-vat})

        elif path == "/api/reports/cashflow":
            uid = self._require_auth()
            if not uid: return
            s, e = qs.get("start_date",["2000-01-01"])[0], qs.get("end_date",["2099-12-31"])[0]
            conn = get_db()
            total = conn.execute("SELECT COALESCE(SUM(total),0) FROM invoices WHERE user_id=? AND type='invoice' AND status='paid' AND issue_date BETWEEN ? AND ?", (uid,s,e)).fetchone()[0]
            outstanding = conn.execute("SELECT COALESCE(SUM(total),0) FROM invoices WHERE user_id=? AND type='invoice' AND status IN ('sent','overdue') AND issue_date BETWEEN ? AND ?", (uid,s,e)).fetchone()[0]
            monthly = [dict(r) for r in conn.execute(
                "SELECT strftime('%Y-%m', COALESCE(payment_date,issue_date)) as month, COALESCE(SUM(total),0) as received, COUNT(*) as count FROM invoices WHERE user_id=? AND type='invoice' AND status='paid' AND issue_date BETWEEN ? AND ? GROUP BY month ORDER BY month DESC LIMIT 12",
                (uid,s,e)).fetchall()]
            by_method = [dict(r) for r in conn.execute(
                "SELECT COALESCE(NULLIF(payment_method,''),'unspecified') as method, COALESCE(SUM(total),0) as amount, COUNT(*) as count FROM invoices WHERE user_id=? AND type='invoice' AND status='paid' AND issue_date BETWEEN ? AND ? GROUP BY method",
                (uid,s,e)).fetchall()]
            conn.close()
            self._send_json({"total_received":total,"outstanding":outstanding,"monthly":monthly,"by_method":by_method})

        elif path == "/api/reports/tax":
            uid = self._require_auth()
            if not uid: return
            s, e = qs.get("start_date",["2000-01-01"])[0], qs.get("end_date",["2099-12-31"])[0]
            conn = get_db()
            def qv(sql): return conn.execute(sql,(uid,s,e)).fetchone()[0]
            taxable   = qv("SELECT COALESCE(SUM(subtotal-discount_amount),0) FROM invoices WHERE user_id=? AND type='invoice' AND issue_date BETWEEN ? AND ?")
            total_vat = qv("SELECT COALESCE(SUM(vat_amount),0) FROM invoices WHERE user_id=? AND type='invoice' AND issue_date BETWEEN ? AND ?")
            collected = qv("SELECT COALESCE(SUM(vat_amount),0) FROM invoices WHERE user_id=? AND type='invoice' AND status='paid' AND issue_date BETWEEN ? AND ?")
            pending   = qv("SELECT COALESCE(SUM(vat_amount),0) FROM invoices WHERE user_id=? AND type='invoice' AND status IN ('sent','overdue') AND issue_date BETWEEN ? AND ?")
            details   = [dict(r) for r in conn.execute("""
                SELECT i.invoice_number, i.issue_date, i.status, i.subtotal, i.vat_amount, i.total, c.name as client_name
                FROM invoices i JOIN clients c ON i.client_id=c.id
                WHERE i.user_id=? AND i.type='invoice' AND i.issue_date BETWEEN ? AND ?
                ORDER BY i.issue_date DESC""", (uid,s,e)).fetchall()]
            conn.close()
            self._send_json({"total_taxable":taxable,"total_vat":total_vat,"vat_collected":collected,"vat_pending":pending,"details":details})

        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/auth/register":
            data = self._body()
            name = data.get("name","").strip()
            email = data.get("email","").strip().lower()
            password = data.get("password","")
            company = data.get("company_name","").strip()
            if not name or not email or not password:
                self._send_json({"error":"Name, email and password are required"},400); return
            if len(password) < 6:
                self._send_json({"error":"Password must be at least 6 characters"},400); return
            conn = get_db()
            if conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
                conn.close(); self._send_json({"error":"An account with this email already exists"},400); return
            uid = str(uuid.uuid4())
            conn.execute("INSERT INTO users (id,name,email,password_hash,company_name) VALUES (?,?,?,?,?)",
                (uid, name, email, hash_password(password), company))
            conn.execute("INSERT INTO settings (user_id,company_name,company_email) VALUES (?,?,?)", (uid,company,email))
            sid = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions (id,user_id,expires_at) VALUES (?,?,?)",
                (sid, uid, (datetime.now()+timedelta(seconds=SESSION_MAX_AGE)).isoformat()))
            conn.commit(); conn.close()
            self._send_json_cookie({"success":True,"user":{"id":uid,"name":name,"email":email,"company_name":company}}, sid)

        elif path == "/api/auth/login":
            data = self._body()
            email = data.get("email","").strip().lower()
            password = data.get("password","")
            if not email or not password:
                self._send_json({"error":"Email and password are required"},400); return
            conn = get_db()
            u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if not u or not verify_password(password, u["password_hash"]):
                conn.close(); self._send_json({"error":"Invalid email or password"},401); return
            sid = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions (id,user_id,expires_at) VALUES (?,?,?)",
                (sid, u["id"], (datetime.now()+timedelta(seconds=SESSION_MAX_AGE)).isoformat()))
            conn.commit(); conn.close()
            self._send_json_cookie({"success":True,"user":{"id":u["id"],"name":u["name"],"email":u["email"],"company_name":u["company_name"]}}, sid)

        elif path == "/api/auth/logout":
            cookies = http.cookies.SimpleCookie(self.headers.get("Cookie",""))
            if SESSION_COOKIE in cookies:
                sid = cookies[SESSION_COOKIE].value
                conn = get_db()
                conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
                conn.commit(); conn.close()
            self._send_json_clear_cookie({"success":True})

        elif path == "/api/settings":
            uid = self._require_auth()
            if not uid: return
            data = self._body(); conn = get_db()
            fields = ["company_name","company_address","company_email","company_phone","tax_number",
                      "bank_name","account_name","account_number",
                      "smtp_host","smtp_port","smtp_user","smtp_password","smtp_from_name","smtp_from_email"]
            if not conn.execute("SELECT id FROM settings WHERE user_id=?", (uid,)).fetchone():
                conn.execute("INSERT INTO settings (user_id) VALUES (?)", (uid,))
            sets = [f"{f}=?" for f in fields if f in data]
            vals = [data[f] for f in fields if f in data]
            if sets:
                vals.append(uid)
                conn.execute(f"UPDATE settings SET {','.join(sets)} WHERE user_id=?", vals)
            conn.commit(); conn.close()
            self._send_json({"success":True})

        elif path == "/api/settings/logo":
            uid = self._require_auth()
            if not uid: return
            ct = self.headers.get("Content-Type","")
            body = self.rfile.read(int(self.headers.get("Content-Length",0)))
            if "multipart/form-data" in ct:
                boundary = ct.split("boundary=")[1].encode()
                for part in body.split(b"--" + boundary):
                    if b"filename=" in part:
                        hend = part.index(b"\r\n\r\n") + 4
                        fdata = part[hend:].rstrip(b"\r\n--")
                        fs = part.index(b'filename="') + 10
                        fe = part.index(b'"', fs)
                        ext = os.path.splitext(part[fs:fe].decode())[1] or ".png"
                        fn = f"logo_{uuid.uuid4().hex[:8]}{ext}"
                        with open(os.path.join(UPLOAD_DIR, fn), "wb") as f: f.write(fdata)
                        conn = get_db()
                        conn.execute("UPDATE settings SET company_logo=? WHERE user_id=?", (fn,uid))
                        conn.commit(); conn.close()
                        self._send_json({"success":True,"filename":fn}); return
            self._send_json({"error":"No file"},400)

        elif path == "/api/clients":
            uid = self._require_auth()
            if not uid: return
            data = self._body(); cid = str(uuid.uuid4()); conn = get_db()
            conn.execute("INSERT INTO clients (id,user_id,name,email,phone,address,company) VALUES (?,?,?,?,?,?,?)",
                (cid,uid,data["name"],data["email"],data.get("phone",""),data.get("address",""),data.get("company","")))
            conn.commit(); conn.close()
            self._send_json({"success":True,"id":cid})

        elif path == "/api/invoices":
            uid = self._require_auth()
            if not uid: return
            data = self._body(); inv_id = str(uuid.uuid4())
            inv_num = generate_invoice_number(data["type"], uid)
            items = data.get("items",[])
            totals = calculate_totals(items, data.get("discount_percent",0))
            conn = get_db()
            conn.execute("""INSERT INTO invoices
                (id,user_id,invoice_number,type,status,client_id,issue_date,due_date,
                 subtotal,vat_amount,discount_percent,discount_amount,total,notes,terms,currency,
                 reminder_enabled,reminder_frequency_days,reminder_max_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (inv_id,uid,inv_num,data["type"],data.get("status","draft"),
                 data["client_id"],data["issue_date"],data["due_date"],
                 totals["subtotal"],totals["vat_amount"],data.get("discount_percent",0),
                 totals["discount_amount"],totals["total"],data.get("notes",""),data.get("terms",""),
                 data.get("currency","NGN"),1 if data.get("reminder_enabled") else 0,
                 data.get("reminder_frequency_days",7),data.get("reminder_max_count",3)))
            for i, item in enumerate(items):
                conn.execute("INSERT INTO invoice_items (id,invoice_id,description,quantity,unit_price,amount,sort_order) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()),inv_id,item["description"],item["quantity"],item["unit_price"],round(item["quantity"]*item["unit_price"],2),i))
            conn.commit(); conn.close()
            self._send_json({"success":True,"id":inv_id,"invoice_number":inv_num})

        elif path.startswith("/api/invoices/") and path.endswith("/send"):
            uid = self._require_auth()
            if not uid: return
            result = send_invoice_email(path.split("/")[-2])
            self._send_json(result, 200 if result["success"] else 500)

        elif path.startswith("/api/invoices/") and path.endswith("/remind"):
            uid = self._require_auth()
            if not uid: return
            result = send_invoice_email(path.split("/")[-2], is_reminder=True)
            self._send_json(result, 200 if result["success"] else 500)

        else:
            self._send_json({"error":"Not found"},404)

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/auth/profile":
            uid = self._require_auth()
            if not uid: return
            data = self._body(); conn = get_db()
            u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if data.get("new_password"):
                if not verify_password(data.get("current_password",""), u["password_hash"]):
                    conn.close(); self._send_json({"error":"Current password is incorrect"},400); return
                conn.execute("UPDATE users SET name=?,email=?,password_hash=? WHERE id=?",
                    (data.get("name",u["name"]), data.get("email",u["email"]), hash_password(data["new_password"]), uid))
            else:
                conn.execute("UPDATE users SET name=?,email=? WHERE id=?",
                    (data.get("name",u["name"]), data.get("email",u["email"]), uid))
            conn.commit(); conn.close()
            self._send_json({"success":True})

        elif path.startswith("/api/clients/"):
            uid = self._require_auth()
            if not uid: return
            cid = path.split("/")[-1]; data = self._body(); conn = get_db()
            conn.execute("UPDATE clients SET name=?,email=?,phone=?,address=?,company=? WHERE id=? AND user_id=?",
                (data["name"],data["email"],data.get("phone",""),data.get("address",""),data.get("company",""),cid,uid))
            conn.commit(); conn.close()
            self._send_json({"success":True})

        elif path.startswith("/api/invoices/") and path.endswith("/status"):
            uid = self._require_auth()
            if not uid: return
            inv_id = path.split("/")[-2]; data = self._body(); conn = get_db()
            sets = ["status=?","updated_at=datetime('now')"]; vals = [data["status"]]
            if data.get("payment_method"): sets.append("payment_method=?"); vals.append(data["payment_method"])
            if data.get("payment_date"):   sets.append("payment_date=?");   vals.append(data["payment_date"])
            elif data["status"] == "paid": sets.append("payment_date=?");   vals.append(datetime.now().strftime("%Y-%m-%d"))
            vals += [inv_id, uid]
            conn.execute(f"UPDATE invoices SET {','.join(sets)} WHERE id=? AND user_id=?", vals)
            conn.commit(); conn.close()
            self._send_json({"success":True})

        elif path.startswith("/api/invoices/"):
            uid = self._require_auth()
            if not uid: return
            inv_id = path.split("/")[-1]; data = self._body()
            items = data.get("items",[]); totals = calculate_totals(items, data.get("discount_percent",0))
            conn = get_db()
            conn.execute("""UPDATE invoices SET client_id=?,issue_date=?,due_date=?,
                subtotal=?,vat_amount=?,discount_percent=?,discount_amount=?,total=?,
                notes=?,terms=?,currency=?,reminder_enabled=?,reminder_frequency_days=?,
                reminder_max_count=?,updated_at=datetime('now') WHERE id=? AND user_id=?""",
                (data["client_id"],data["issue_date"],data["due_date"],
                 totals["subtotal"],totals["vat_amount"],data.get("discount_percent",0),
                 totals["discount_amount"],totals["total"],data.get("notes",""),data.get("terms",""),
                 data.get("currency","NGN"),1 if data.get("reminder_enabled") else 0,
                 data.get("reminder_frequency_days",7),data.get("reminder_max_count",3),inv_id,uid))
            conn.execute("DELETE FROM invoice_items WHERE invoice_id=?", (inv_id,))
            for i, item in enumerate(items):
                conn.execute("INSERT INTO invoice_items (id,invoice_id,description,quantity,unit_price,amount,sort_order) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()),inv_id,item["description"],item["quantity"],item["unit_price"],round(item["quantity"]*item["unit_price"],2),i))
            conn.commit(); conn.close()
            self._send_json({"success":True})

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        uid = self._require_auth()
        if not uid: return
        conn = get_db()
        if path.startswith("/api/clients/"):
            conn.execute("DELETE FROM clients WHERE id=? AND user_id=?", (path.split("/")[-1],uid))
        elif path.startswith("/api/invoices/"):
            conn.execute("DELETE FROM invoices WHERE id=? AND user_id=?", (path.split("/")[-1],uid))
        conn.commit(); conn.close()
        self._send_json({"success":True})

    def log_message(self, *a): pass


def main():
    init_db()
    ReminderScheduler().start()
    srv = http.server.HTTPServer(("0.0.0.0", PORT), InvoiceHandler)
    print(f"\n  FlowLedger is running →  http://localhost:{PORT}\n  Press Ctrl+C to stop.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped."); srv.server_close()

if __name__ == "__main__":
    main()
