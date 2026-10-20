"""Regenerate fictional English-language USD invoice and PO sample documents."""
from pathlib import Path
import fitz
from PIL import Image, ImageDraw, ImageFont
from app.demo_dataset import VENDORS, calculate_invoice_amounts, invoice_text

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'examples' / 'invoices'
PO = ROOT / 'examples' / 'purchase_orders'
OUT.mkdir(parents=True, exist_ok=True)
PO.mkdir(parents=True, exist_ok=True)
ISSUE_DATE = '2026-10-14'
DUE_DATE = '2026-11-13'


def make_invoice(vendor_index: int, invoice_number: str, po_number: str, amounts: dict,
                 *, account: str | None = None, malicious: bool = False) -> str:
    vendor = VENDORS[vendor_index]
    return invoice_text(
        invoice_number=invoice_number,
        issue_date=ISSUE_DATE,
        due_date=DUE_DATE,
        vendor=vendor,
        po_number=po_number,
        bank_name=vendor['bank'],
        account_number=account or vendor['account'],
        account_holder=vendor['name'],
        amounts=amounts,
        malicious=malicious,
    )


green = make_invoice(0, 'INV-MVP-GREEN-0001', 'PO-2026-0001', calculate_invoice_amounts(1, 0))
yellow = make_invoice(0, 'INV-MVP-YELLOW-0001', 'PO-2026-0001', calculate_invoice_amounts(1, 0), account='999900000001')
red = make_invoice(0, 'INV-MVP-RED-0001', 'PO-2026-0001', calculate_invoice_amounts(1, 0), malicious=True)
# The mismatch example references the bulk supplier's PO but adds a large unapproved line item.
mismatch = make_invoice(5, 'INV-MVP-PO-MISMATCH-0001', 'PO-2026-0048', calculate_invoice_amounts(48, 5, include_unapproved_item=True))

for filename, text in [
    ('01_invoice_green.pdf', green),
    ('02_invoice_yellow_new_account.pdf', yellow),
    ('03_invoice_red_prompt_injection.pdf', red),
    ('04_invoice_po_mismatch.pdf', mismatch),
]:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((48, 48), 'SENTRION MVP — SYNTHETIC SAMPLE DOCUMENT', fontsize=8, color=(0.25, 0.4, 0.6))
    page.insert_textbox(fitz.Rect(48, 72, 548, 800), text, fontsize=10, fontname='helv', lineheight=1.35)
    doc.save(OUT / filename)
    doc.close()

# Scanned/image invoice sample to exercise the existing OCR workflow.
font_candidates = [
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf',
    'C:/Windows/Fonts/arial.ttf',
]
font_path = next((p for p in font_candidates if Path(p).exists()), None)
font = ImageFont.truetype(font_path, 30) if font_path else ImageFont.load_default()
img = Image.new('RGB', (1700, 2200), 'white')
draw = ImageDraw.Draw(img)
y = 100
for line in yellow.splitlines():
    draw.text((100, y), line, font=font, fill=(25, 35, 45))
    y += 58
img.save(OUT / '05_invoice_scanned_yellow.png', dpi=(220, 220))

po_amounts = calculate_invoice_amounts(1, 0)
po_vendor = VENDORS[0]
po_lines = [
    'PURCHASE ORDER',
    'Purchase Order Number: PO-UPLOAD-8888',
    'Currency: USD',
    f'Vendor: {po_vendor["name"]}',
    f'Vendor Address: {po_vendor["address"]}',
    f'Tax ID: {po_vendor["tax_id"]}',
    f'Issue Date: {ISSUE_DATE}',
    f'Subtotal: {po_amounts["subtotal"]:,}',
    f'Tax: {po_amounts["tax"]:,}',
    f'Total: {po_amounts["total"]:,}',
]
for item in po_amounts['items']:
    po_lines.append(
        f'ITEM: {item["item_name"]} | QTY: {item["quantity"]} | UNIT: {item["unit"]} '
        f'| UNIT PRICE: {item["unit_price"]} | TAX: {item["tax_rate"]}% | TOTAL: {item["line_total"]}'
    )
po_text = '\n'.join(po_lines)
doc = fitz.open()
page = doc.new_page(width=595, height=842)
page.insert_textbox(fitz.Rect(48, 50, 548, 800), po_text, fontsize=10, fontname='helv', lineheight=1.35)
doc.save(PO / '01_purchase_order_upload_example.pdf')
doc.close()

print('Generated fictional English-language USD examples:')
for path in sorted(list(OUT.iterdir()) + list(PO.iterdir())):
    print(f'- {path.relative_to(ROOT)}')
