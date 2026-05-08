# Naija Invoice

A simple invoicing tool for Nigerian businesses. Generate invoices and quotes, send them via email, and set up automated payment reminders — all with Nigerian VAT (7.5%) built in.

## Features

- **Invoices & Quotes** — create professional invoices for completed work or quotations for proposed work
- **Nigerian VAT (7.5%)** — automatically calculated per FIRS tax law
- **Email Sending** — send invoices directly to clients with the invoice attached
- **Payment Reminders** — automated or manual reminders with configurable frequency (3, 7, 14, 30 days or custom)
- **Company Branding** — upload your company logo (click or drag-and-drop)
- **Client Management** — store and manage client details
- **Bank Details** — display your payment info on invoices (bank name, account name, account number)
- **Dashboard** — overview of invoices, quotes, paid and outstanding amounts
- **Zero Dependencies** — runs on Python 3 with no external packages needed

## Quick Start

1. Make sure you have Python 3 installed:
   ```
   python3 --version
   ```

2. Clone this repo and run the server:
   ```
   git clone https://github.com/tbabsjonah/naija-invoice-.git
   cd naija-invoice
   python3 server.py
   ```

3. Open **http://localhost:8080** in your browser

4. Go to **Settings** to:
   - Add your company name, address, and logo
   - Enter your bank details (shown on invoices)
   - Configure email (SMTP) to send invoices

## Email Setup (Gmail)

To send invoices via email using Gmail:

| Setting     | Value              |
|-------------|--------------------|
| SMTP Host   | smtp.gmail.com     |
| SMTP Port   | 587                |
| Username    | your@gmail.com     |
| Password    | App Password*      |

*You need a [Google App Password](https://support.google.com/accounts/answer/185833), not your regular Gmail password.

## How It Works

1. **Add Clients** — go to Clients and add your customer details
2. **Create Invoice/Quote** — select a client, add line items, VAT is auto-calculated
3. **Send** — email the invoice to the client with one click
4. **Track** — monitor payment status (draft, sent, paid, overdue)
5. **Remind** — send manual reminders or enable auto-reminders

## Screenshots

### Dashboard
The dashboard shows your invoice and quote totals, paid and outstanding amounts, and recent activity.

### Invoice Preview
Professional invoice layout with your company logo, client details, itemized services, VAT breakdown, and bank payment info.

## Tech Stack

- **Backend**: Python 3 (standard library only — no pip install needed)
- **Database**: SQLite (auto-created on first run)
- **Frontend**: HTML, CSS, JavaScript (single-page app, no framework)

## License

MIT
