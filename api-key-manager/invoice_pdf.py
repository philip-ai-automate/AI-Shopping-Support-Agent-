import os
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from portal_utils import money_fmt


def ensure_invoice_dir() -> str:
    base = os.getenv("INVOICE_DIR", "/root/api-key-manager/invoices")
    os.makedirs(base, exist_ok=True)
    return base


def generate_invoice_pdf(
    invoice_number: str,
    customer_email: str,
    tenant_name: str,
    credits: int,
    amount_pence: int,
    vat_pence: int,
    currency: str,
    created_at: datetime,
) -> str:
    """Returns absolute file path."""

    out_dir = ensure_invoice_dir()
    filename = f"{invoice_number}.pdf"
    path = os.path.join(out_dir, filename)

    c = canvas.Canvas(path, pagesize=A4)
    width, height = A4

    # Brand header
    c.setFont("Helvetica-Bold", 20)
    c.drawString(40, height - 60, "PhiXtra")
    c.setFont("Helvetica", 11)
    c.drawString(40, height - 80, "AI Shopping Agent")

    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(width - 40, height - 60, "INVOICE")

    c.setFont("Helvetica", 10)
    c.drawRightString(width - 40, height - 80, f"Invoice: {invoice_number}")
    c.drawRightString(width - 40, height - 95, f"Date: {created_at.strftime('%Y-%m-%d')}")

    # Bill to
    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, height - 130, "Bill To")
    c.setFont("Helvetica", 10)
    c.drawString(40, height - 145, customer_email)
    c.drawString(40, height - 160, f"Tenant: {tenant_name}")

    # Table header
    y = height - 210
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "Description")
    c.drawRightString(width - 40, y, "Amount")
    c.line(40, y - 5, width - 40, y - 5)

    # Line item
    y -= 25
    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"Credit top-up: {credits} credits (1 credit = 5,000 tokens)")
    c.drawRightString(width - 40, y, money_fmt(amount_pence, currency))

    # Totals
    y -= 40
    c.line(40, y + 15, width - 40, y + 15)

    subtotal = amount_pence
    total = amount_pence + int(vat_pence)

    c.setFont("Helvetica", 10)
    c.drawRightString(width - 40, y, f"Subtotal: {money_fmt(subtotal, currency)}")
    y -= 15
    c.drawRightString(width - 40, y, f"VAT: {money_fmt(vat_pence, currency)}")
    y -= 18
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(width - 40, y, f"Total: {money_fmt(total, currency)}")

    # Footer
    c.setFont("Helvetica", 9)
    c.drawString(40, 60, "Thank you for your business.")
    c.setFont("Helvetica", 8)
    c.drawString(40, 45, "Support: support@phixtra.com")

    c.showPage()
    c.save()

    return path
