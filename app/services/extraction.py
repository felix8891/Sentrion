from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Any

from ..currency import DEFAULT_CURRENCY, detect_currency, parse_money_amount

MALICIOUS_PATTERNS = [
    re.compile(r'ignore\s+(all\s+)?previous\s+instructions', re.I),
    re.compile(r'bypass\s+(all\s+)?(security|verification|approval|policy|policies|checks)', re.I),
    re.compile(r'(ai\s+agent|system\s+instruction|assistant)\s*[:\-].{0,100}(execute|approve|ignore|override|reveal|change)', re.I | re.S),
    re.compile(r'(override|disable)\s+(the\s+)?(security|policy|approval|guardrail)', re.I),
    re.compile(r'reveal\s+(confidential|sensitive|secret)', re.I),
    re.compile(r'call\s+(the\s+)?(privileged\s+)?payment\s+tool', re.I),
]

# Label variants commonly found on supplier invoices. These patterns intentionally
# prefer explicit labels over guessing from a document header.
_AMOUNT_PREFIX = r'(?:USD|IDR|EUR|GBP|JPY|KRW|CAD|AUD|SGD|CNY|INR|MYR|THB|PHP|VND|CHF|NZD|HKD|AED|SAR|US\$|CA\$|AU\$|S\$|Rp\.?|[$€£¥₹])?\s*'
_AMOUNT_VALUE = _AMOUNT_PREFIX + r'\(?([-+]?[0-9][0-9., ]*[0-9]|[-+]?[0-9])\)?'
_DATE_VALUE = r'(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})'


def detect_ai_directed_instruction(text: str) -> list[str]:
    hits = []
    for pattern in MALICIOUS_PATTERNS:
        match = pattern.search(text or '')
        if match:
            hits.append(match.group(0)[:180].replace('\n', ' '))
    return hits


def parse_amount(value: str | None, currency: str | None = None) -> float | None:
    """Backward-compatible wrapper for currency-aware monetary parsing."""
    return parse_money_amount(value, currency or DEFAULT_CURRENCY)


def parse_number(value: str | None) -> float | None:
    if value is None:
        return None
    source = str(value).strip().replace(' ', '')
    if not source:
        return None
    match = re.search(r'-?[0-9][0-9.,]*', source)
    if not match:
        return None
    token = match.group(0)
    if ',' in token and '.' in token:
        last = max(token.rfind(','), token.rfind('.'))
        trailing = len(token) - last - 1
        if 1 <= trailing <= 3:
            token = re.sub(r'[,.]', '', token[:last]) + '.' + token[last + 1:]
        else:
            token = re.sub(r'[,.]', '', token)
    elif ',' in token or '.' in token:
        separator = ',' if ',' in token else '.'
        pieces = token.split(separator)
        if len(pieces) > 2 or len(pieces[-1]) == 3:
            token = ''.join(pieces)
        else:
            token = ''.join(pieces[:-1]) + '.' + pieces[-1]
    try:
        return float(token)
    except ValueError:
        return None


def _capture(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.M)
        if match:
            value = match.group(1).strip().strip('|').strip()
            return value or None
    return None


def _clean_identifier(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().strip('|;,: ')
    # Some PDFs flatten multiple columns into a single line. Stop an identifier at
    # the next explicit field label rather than storing the rest of that row in it.
    value = re.split(
        r'\s+(?=(?:invoice\s+date|issue\s+date|date\s+of\s+issue|due\s+date|payment\s+due|currency|vendor|supplier|tax\s*id|vat\s*(?:no|id)|gstin|purchase\s+order|po\s*(?:no|number)|subtotal|discount|grand\s+total|invoice\s+total|amount\s+due)\s*[:#=\-])',
        value,
        maxsplit=1,
        flags=re.I,
    )[0]
    return value.strip().strip('.|;,: ') or None


def _normalize_date(value: str | None) -> str | None:
    """Normalize explicit dates; reject ambiguous numeric dates instead of guessing."""
    if not value:
        return None
    from datetime import datetime

    candidate = value.strip().replace(',', '')
    formats = (
        '%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%d.%m.%Y', '%d %B %Y', '%d %b %Y',
        '%B %d %Y', '%b %d %Y',
    )
    for fmt in formats:
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue

    match = re.fullmatch(r'(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})', candidate)
    if match:
        first, second, year = (int(part) for part in match.groups())
        if '.' in candidate:
            try:
                return datetime(year, second, first).date().isoformat()
            except ValueError:
                return None
        if year < 100:
            year += 2000 if year < 70 else 1900
        # Determine day/month order only when one position cannot be a month.
        if first > 12 and 1 <= second <= 12:
            day, month = first, second
        elif second > 12 and 1 <= first <= 12:
            month, day = first, second
        elif first == second and 1 <= first <= 12:
            day = month = first
        else:
            return None
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None
    return None


def _has_usable_pdf_text(text: str) -> bool:
    source = (text or '').strip()
    if len(source) < 80:
        return False
    words = re.findall(r'[A-Za-z0-9]{2,}', source)
    alphanumeric = sum(ch.isalnum() for ch in source)
    return len(words) >= 8 and alphanumeric >= 50


def _ocr_pil_image(image) -> str:
    """OCR a normalized image with a safe language fallback."""
    from PIL import ImageOps, ImageEnhance
    import pytesseract

    normalized = ImageOps.autocontrast(ImageOps.grayscale(image))
    if max(normalized.size) < 1600:
        scale = min(2.0, 1600 / max(normalized.size))
        normalized = normalized.resize((int(normalized.width * scale), int(normalized.height * scale)))
    normalized = ImageEnhance.Contrast(normalized).enhance(1.25)
    try:
        return pytesseract.image_to_string(normalized, lang='ind+eng', config='--psm 6')
    except Exception:
        return pytesseract.image_to_string(normalized, lang='eng', config='--psm 6')


def _nonempty_lines(text: str) -> list[str]:
    return [re.sub(r'\s+', ' ', line).strip() for line in (text or '').replace('\r', '\n').splitlines() if line.strip()]


def _exact_line_index(lines: list[str], pattern: str) -> int | None:
    for index, line in enumerate(lines):
        if re.fullmatch(pattern, line, re.I):
            return index
    return None


def _value_after_label(lines: list[str], label_pattern: str) -> str | None:
    """Read a labelled field whether its value is inline or on the following line."""
    for index, line in enumerate(lines):
        match = re.match(rf'^\s*(?:{label_pattern})\s*(?:[:#=]\s*(.*))?\s*$', line, re.I)
        if not match:
            continue
        inline = (match.group(1) or '').strip()
        if inline:
            return inline
        if index + 1 < len(lines):
            candidate = lines[index + 1].strip()
            # If a section heading is followed by a second explicit label, keep
            # scanning so the labelled value is captured instead of the label text.
            if re.match(rf'^(?:{label_pattern})\s*[:#=]', candidate, re.I):
                continue
            if candidate and not re.fullmatch(r'(?:SUPPLIER|VENDOR|BILL TO|PURCHASE ORDER|PAYMENT TERMS|CURRENCY|NOTES|No\.|Product / Pack|SKU|Unit|Qty|Unit Price|Line Total|Discount|GST)', candidate, re.I):
                return candidate
    return None


def _section_party(lines: list[str], header_pattern: str, stop_patterns: tuple[str, ...]) -> tuple[str | None, str | None, int | None]:
    """Extract a party's name and address from a labelled block such as SUPPLIER/BILL TO."""
    start = _exact_line_index(lines, header_pattern)
    if start is None or start + 1 >= len(lines):
        return None, None, start
    name_index = start + 1
    name = lines[name_index]
    # Do not mistake the next section label for a company name.
    if any(re.fullmatch(pattern, name, re.I) for pattern in stop_patterns):
        return None, None, start
    address_parts: list[str] = []
    for line in lines[name_index + 1:]:
        if any(re.fullmatch(pattern, line, re.I) for pattern in stop_patterns):
            break
        if re.match(r'^(?:tax\s*(?:registration|id|identification)|vat\s*(?:registration\s*)?(?:id|no|number)|gstin|npwp|tin)\b', line, re.I):
            break
        # Do not let later invoice metadata leak into the address.
        if re.match(r'^(?:purchase\s*order|payment\s*terms|currency|subtotal|item\s*discounts?|taxable\s*amount|gst\s*total|shipping|other\s*charges|rounding|amount\s*due|payment\s*reference)\b', line, re.I):
            break
        address_parts.append(line)
    address = ' '.join(address_parts).strip() or None
    return name, address, start


def _party_tax_ids(lines: list[str], bill_to_index: int | None) -> tuple[str | None, str | None]:
    tax_lines: list[tuple[int, str]] = []
    label = re.compile(r'^(?:tax\s*(?:reg(?:istration)?(?:\s*(?:no\.?|number|id))?|identification(?:\s*(?:number|no\.?))?|id|no\.?|number)|vat\s*(?:reg(?:istration)?\s*)?(?:id|no\.?|number)|gstin|npwp|tin)\s*[.:#=\-]?\s*(.*?)\s*$', re.I)
    for i, line in enumerate(lines):
        match = label.match(line)
        if not match:
            continue
        value = match.group(1).strip()
        if not value and i + 1 < len(lines):
            value = lines[i + 1].strip()
        if value:
            tax_lines.append((i, _clean_identifier(value) or value))
    if not tax_lines:
        return None, None
    if bill_to_index is None:
        # In table-style headers (e.g. SUPPLIER / FROM | CUSTOMER / BILL TO),
        # the role headers may appear before both identity blocks. In that layout,
        # tax identifiers remain ordered supplier first, buyer second.
        return tax_lines[0][1], (tax_lines[1][1] if len(tax_lines) > 1 else None)
    supplier_ids = [value for index, value in tax_lines if index < bill_to_index]
    buyer_ids = [value for index, value in tax_lines if index > bill_to_index]
    return (supplier_ids[0] if supplier_ids else None, buyer_ids[0] if buyer_ids else None)


def _summary_amount(lines: list[str], label_pattern: str, currency: str, *, absolute: bool = False) -> float | None:
    """Capture a summary amount, including layouts where label and amount are separate lines."""
    pattern = re.compile(rf'^\s*(?:{label_pattern})\s*(?::|=)?\s*(.*?)\s*$', re.I)
    for i, line in enumerate(lines):
        # Exclude tax-identification labels from summary-tax matching (e.g. Tax Registration No.).
        if re.match(r'^(?:tax\s*(?:reg(?:istration)?|registration|identification|id|no\.?|number)|vat\s*(?:reg(?:istration)?|registration|id|no\.?|number)|gstin|gst\s*(?:registration|id|no\.?|number))\b', line, re.I):
            continue
        match = pattern.match(line)
        if not match:
            continue
        inline = (match.group(1) or '').strip()
        # A fully matched label line has no amount on it; use the next line.
        candidate = inline if re.search(r'\d', inline) else (lines[i + 1] if i + 1 < len(lines) else '')
        # A label like 'VAT (11.00%)' contains a rate, not the tax amount. In vertical
        # extraction the actual currency amount is on the following line.
        if '%' in inline and not re.search(r'(?:USD|IDR|EUR|GBP|JPY|KRW|CAD|AUD|SGD|CNY|INR|MYR|THB|PHP|VND|CHF|NZD|HKD|AED|SAR|US\$|CA\$|AU\$|S\$|Rp\.?|[$€£¥₹])', inline, re.I):
            candidate = lines[i + 1] if i + 1 < len(lines) else ''
        amount = parse_amount(candidate, currency)
        if amount is not None:
            return abs(amount) if absolute else amount
    return None


def _extract_vertical_items(lines: list[str], currency: str) -> list[dict[str, Any]]:
    """Extract vertical and generated procurement table layouts conservatively.

    The v4 dataset emits one value per text line after a seven-column header:
    description, SKU, quantity, unit, unit price, line discount, line amount.
    Keep document amounts as printed; only compute a separate discount amount when
    the source explicitly provides a percentage discount.
    """
    normalized = [re.sub(r'\s+', ' ', line).strip() for line in lines]
    # Current supplier fixture format: No. / Product-Pack / SKU / Unit / Qty ...
    header = _exact_line_index(normalized, r'No\.?')
    if header is not None:
        expected_headers = (r'Product\s*/\s*Pack', r'SKU', r'Unit', r'Qty', r'Unit Price', r'Line Total', r'Discount', r'GST')
        if header + len(expected_headers) < len(normalized) and all(
            re.fullmatch(pattern, normalized[header + offset + 1], re.I) for offset, pattern in enumerate(expected_headers)
        ):
            items: list[dict[str, Any]] = []
            index = header + 1 + len(expected_headers)
            while index + 8 < len(normalized):
                if not re.fullmatch(r'\d{1,3}', normalized[index]):
                    index += 1
                    continue
                item_no = int(normalized[index])
                name, sku, unit = normalized[index + 1:index + 4]
                quantity = parse_number(normalized[index + 4])
                unit_price = parse_amount(normalized[index + 5], currency)
                line_total = parse_amount(normalized[index + 6], currency)
                item_discount = parse_amount(normalized[index + 7], currency)
                item_tax = parse_amount(normalized[index + 8], currency)
                sku_is_plausible = bool(re.fullmatch(r'[A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)+', sku))
                unit_is_plausible = bool(re.fullmatch(r'[A-Za-z][A-Za-z -]{0,24}', unit))
                if (item_no == len(items) + 1 and name and sku_is_plausible and unit_is_plausible
                        and quantity is not None and unit_price is not None and line_total is not None
                        and item_discount is not None and item_tax is not None):
                    items.append({'item_name': name.strip(), 'description': None, 'sku': sku.strip(),
                                  'quantity': quantity, 'unit': unit.strip(), 'unit_price': unit_price,
                                  'tax_rate': None, 'item_tax': item_tax, 'line_total': line_total,
                                  'item_discount': abs(item_discount), 'item_discount_source': 'DOCUMENT_AMOUNT'})
                    index += 9
                else:
                    index += 1
            if items:
                return items

    # Dataset v4 uses both seven-field tables and continuation pages with five-field
    # tables (description, SKU, quantity, unit price, amount). Parse each table by
    # its header and anchor rows at SKU identifiers so a malformed row cannot shift
    # all subsequent columns.
    header_sequences = [
        ('seven', (r'description', r'sku', r'qty', r'unit', r'unit price', r'line disc\.?', r'amount')),
        ('seven', (r'item description', r'sku', r'quantity', r'unit', r'unit price', r'discount', r'amount')),
        ('seven', (r'product\s*/\s*pack', r'sku', r'qty', r'unit', r'unit price', r'line disc\.?', r'amount')),
        ('five', (r'description', r'sku', r'qty', r'unit price', r'amount')),
        ('five', (r'item description', r'sku', r'quantity', r'unit price', r'(?:line total|amount)')),
    ]
    headers: list[tuple[int, int, str]] = []
    for hidx in range(len(normalized)):
        for kind, seq in header_sequences:
            if hidx + len(seq) <= len(normalized) and all(
                re.fullmatch(pattern, normalized[hidx + offset], re.I) for offset, pattern in enumerate(seq)
            ):
                headers.append((hidx, hidx + len(seq), kind))
                break
    if not headers:
        return []

    items: list[dict[str, Any]] = []
    sku_pattern = re.compile(r'[A-Z0-9]+(?:[-_/][A-Z0-9]+)+', re.I)
    summary_line = re.compile(r'^(?:subtotal|discount|tax|gst|vat|shipping|freight|other charges|grand total|po total|invoice total|amount due|remittance|notes|continued on page|page \d)', re.I)
    for hpos, (header_index, start_index, kind) in enumerate(headers):
        end_index = headers[hpos + 1][0] if hpos + 1 < len(headers) else len(normalized)
        cursor = start_index
        while cursor < end_index:
            if summary_line.match(normalized[cursor]):
                break
            if not sku_pattern.fullmatch(normalized[cursor]):
                cursor += 1
                continue
            sku = normalized[cursor]
            name = normalized[cursor - 1] if cursor > start_index else ''
            # A continuation table often omits the unit and discount columns.
            if kind == 'five':
                if cursor + 3 >= end_index:
                    cursor += 1
                    continue
                qty_raw, price_raw, amount_raw = normalized[cursor + 1:cursor + 4]
                unit = None
                discount_raw = None
                consumed = 4
            else:
                if cursor + 5 >= end_index:
                    cursor += 1
                    continue
                qty_raw, unit, price_raw, discount_raw, amount_raw = normalized[cursor + 1:cursor + 6]
                consumed = 6
            if not name or summary_line.match(name) or re.fullmatch(r'(?:SKU|QTY|QUANTITY)', sku, re.I):
                cursor += 1
                continue
            quantity = parse_number(qty_raw)
            unit_price = parse_amount(price_raw, currency)
            line_total = parse_amount(amount_raw, currency)
            if quantity is None or quantity <= 0 or unit_price is None:
                cursor += 1
                continue
            # Preserve an item even if its printed line amount is unreadable; never
            # shift the next row to compensate for a bad field.
            item_discount_source = 'MISSING'
            if kind == 'five':
                gross = quantity * unit_price
                tolerance = 0.5 if currency in {'IDR','JPY','KRW','VND','THB'} else 0.02
                if line_total is not None and gross >= line_total - tolerance:
                    item_discount = round(max(0.0, gross - line_total), 0 if currency in {'IDR','JPY','KRW','VND','THB'} else 2)
                    item_discount_source = 'DERIVED_FROM_UNIT_PRICE_AND_PRINTED_LINE_AMOUNT'
                else:
                    item_discount = None
            else:
                discount_rate = parse_number(discount_raw.rstrip('% ')) if discount_raw and '%' in discount_raw else None
                if discount_raw and '%' in discount_raw and discount_rate is not None:
                    item_discount = round(quantity * unit_price * discount_rate / 100.0, 0 if currency in {'IDR','JPY','KRW','VND','THB'} else 2)
                    item_discount_source = 'DERIVED_FROM_PRINTED_DISCOUNT_RATE'
                elif discount_raw is not None and discount_raw.strip() in {'-', '—', '–', ''}:
                    item_discount = 0.0
                    item_discount_source = 'DOCUMENT_ZERO_OR_DASH'
                else:
                    item_discount = parse_amount(discount_raw, currency) if discount_raw is not None else None
                    item_discount = abs(item_discount) if item_discount is not None else None
                    item_discount_source = 'DOCUMENT_AMOUNT' if item_discount is not None else 'MISSING'
            items.append({
                'item_name': name.strip(), 'description': name.strip(), 'sku': sku.strip(),
                'quantity': quantity, 'unit': unit.strip() if unit else None,
                'unit_price': unit_price, 'tax_rate': None, 'item_tax': None,
                'line_total': line_total, 'item_discount': item_discount,
                'item_discount_source': item_discount_source,
            })
            cursor += consumed
    # De-duplicate OCR/native-text repeats while preserving first appearance.
    unique: list[dict[str, Any]] = []
    seen_skus: set[str] = set()
    for item in items:
        key = (item.get('sku') or '').casefold()
        if key and key in seen_skus:
            continue
        if key:
            seen_skus.add(key)
        unique.append(item)
    return unique


def _extract_party_records(lines: list[str]) -> list[dict[str, str | None]]:
    """Extract party identity blocks from dataset and common Tax/VAT registration labels."""
    records: list[dict[str, str | None]] = []
    tax_re = re.compile(
        r'^(?:tax\s*(?:reg(?:istration)?(?:\s*(?:no\.?|number|id))?|registration(?:\s*(?:no\.?|number|id))?|identification(?:\s*(?:no\.?|number|id))?|id|no\.?|number)|'
        r'vat\s*(?:reg(?:istration)?\s*)?(?:no\.?|number|id)|gstin|gst\s*(?:no\.?|number)|npwp|tin|ein|nomor\s*pajak)\s*[:#=\-]?\s*(.*?)\s*$',
        re.I,
    )
    for idx, line in enumerate(lines):
        match = tax_re.match(line.strip())
        if not match:
            continue
        tax_id = _clean_identifier(match.group(1))
        if not tax_id and idx + 1 < len(lines):
            tax_id = _clean_identifier(lines[idx + 1])
        if idx < 2:
            continue
        name = lines[idx - 2].strip()
        address = lines[idx - 1].strip()
        if re.fullmatch(r'(?:supplier|vendor|seller|buyer|bill to|customer|buyer / purchasing entity|supplier / vendor|supplier / from|customer / bill to)', name, re.I):
            continue
        if re.match(r'^(?:bank|account|email|contact|procurement ref|payment terms|currency|po date|invoice date|due date)', name, re.I):
            continue
        records.append({'name': name or None, 'address': address or None, 'tax_id': tax_id})
    # Keep ordered unique tax anchors; duplicated OCR layer labels must not shift the party roles.
    unique: list[dict[str, str | None]] = []
    seen = set()
    for item in records:
        marker = ((item.get('tax_id') or '').casefold(), (item.get('name') or '').casefold())
        if marker not in seen:
            seen.add(marker)
            unique.append(item)
    return unique


def _document_date(text: str, lines: list[str], labels: str, currency: str) -> str | None:
    raw = _value_after_label(lines, labels)
    if not raw:
        raw = _capture(text, [rf'(?:{labels})\s*[:#=\-]?\s*{_DATE_VALUE}'])
    parsed = _normalize_date(raw)
    if parsed or not raw:
        return parsed
    # Dataset-issued numeric dates are day-first for non-US currency families; preserve
    # the general parser's conservative behavior for ambiguous USD dates.
    candidate = raw.strip()
    match = re.fullmatch(r'(\d{1,2})[-/.](\d{1,2})[-/](\d{4})', candidate)
    if match and ('.' in candidate or currency in {'IDR','SGD','MYR','THB','AUD','EUR','GBP','JPY','CNY'}):
        from datetime import date
        day, month, year = map(int, match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def _header_identifier(lines: list[str], title_pattern: str) -> str | None:
    """Fallback for generated PDFs that print the document number below the title."""
    for i, line in enumerate(lines):
        if re.fullmatch(title_pattern, line.strip(), re.I):
            for candidate in lines[i + 1:i + 4]:
                candidate = _clean_identifier(candidate)
                if candidate and re.search(r'\d', candidate) and re.fullmatch(r'[A-Z0-9][A-Z0-9./_-]{3,}', candidate, re.I):
                    return candidate
    return None

def extract_fields(text: str, received_at: str, original_filename: str = '', document_type: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    text = (text or '').replace('\r', '\n')
    # Normalize horizontal whitespace but preserve line breaks; these are meaningful in native PDF table extraction.
    text = re.sub(r'[ \t]+', ' ', text)
    lines = _nonempty_lines(text)

    invoice_no = _capture(text, [
        r'(?:tax\s+invoice\s*(?:number|no\.?|#|id)?|invoice\s*(?:number|no\.?|#|id)|bill\s*(?:number|no\.?|#)|document\s*(?:number|no\.?|#)|nomor\s*invoice|no\.?\s*invoice)\s*[:#=\-]?\s*([^\n|]+)',
        r'\binvoice\s+id\s*[:#=\-]?\s*([^\n|]+)',
    ])
    supplier_name, supplier_address, supplier_index = _section_party(
        lines, r'SUPPLIER', (r'BILL TO', r'PURCHASE ORDER', r'PAYMENT TERMS', r'CURRENCY', r'No\.?')
    )
    buyer_name, buyer_address, buyer_index = _section_party(
        lines, r'BILL TO', (r'SUPPLIER', r'PURCHASE ORDER', r'PAYMENT TERMS', r'CURRENCY', r'No\.?')
    )
    vendor_name = supplier_name or _capture(text, [
        r'(?:vendor|supplier|seller|vendor\s*name|supplier\s*name|company\s*name|nama\s*(?:perusahaan|vendor|supplier)|billed\s*by|issued\s*by)\s*[:\-]\s*([^\n|]+)',
        r'(?:vendor|supplier|seller)\s+name\s+([^\n|]+)',
    ])
    address = supplier_address or _capture(text, [
        r'(?:vendor\s*address|supplier\s*address|company\s*address|alamat\s*(?:vendor|perusahaan|supplier))\s*[:\-]\s*([^\n|]+)',
    ])
    vendor_tax_id, buyer_tax_id = _party_tax_ids(lines, buyer_index)
    if vendor_tax_id is None:
        vendor_tax_id = _capture(text, [
            r'\b(?:npwp|tax\s*(?:reg(?:istration)?(?:\s*(?:no\.?|number|id))?|id|identification(?:\s*(?:number|no\.?))?|registration(?:\s*(?:number|no\.?))?)|vat\s*(?:registration\s*)?(?:id|no\.?|number)|gstin|gst\s*(?:no\.?|number|registration\s*(?:no\.?|number))|tin|ein|nomor\s*pajak)\s*[:#=\-]?\s*([^\n|]+)',
        ])
    po_no = _value_after_label(lines, r'(?:PURCHASE\s*ORDER\s*(?:NUMBER|NO\.?|#|REFERENCE|REF)?|PO\s*(?:NUMBER|NO\.?|#|REFERENCE|REF)|NOMOR\s*PO|NO\.?\s*PO)')
    if po_no is None:
        po_no = _capture(text, [r'(?:purchase\s*order\s*(?:number|no\.?|#)?|po\s*(?:number|no\.?|#)|nomor\s*po|no\.?\s*po)\s*[:#=\-]\s*([^\n|]+)'])

    issue_raw = _capture(text, [
        rf'(?:invoice\s*date|date\s*of\s*(?:issue|invoice)|issue\s*date|tanggal\s*invoice|tanggal\s*terbit)\s*[:#=\-]?\s*{_DATE_VALUE}',
    ])
    due_raw = _capture(text, [
        rf'(?:payment\s*due(?:\s*date)?|due\s*date|payment\s*deadline|jatuh\s*tempo)\s*[:#=\-]?\s*{_DATE_VALUE}',
    ])
    bank = _capture(text, [r'(?:bank\s*(?:name|details)?|nama\s*bank)\s*[:#=\-]\s*([^\n|]+)'])
    account = _capture(text, [
        r'(?:bank\s*)?(?:account\s*(?:number|no\.?|#)|account\s*number|nomor\s*rekening|no\.?\s*rekening)\s*[:#=\-]?\s*([0-9\- ]{5,})',
        r'(?:rekening)\s*[:#=\-]?\s*([0-9\- ]{5,})',
    ])
    holder = _capture(text, [r'(?:account\s*holder(?:\s*name)?|beneficiary\s*name|atas\s*nama|nama\s*pemilik\s*rekening)\s*[:#=\-]\s*([^\n|]+)'])

    currency, currency_confidence = detect_currency(text)
    subtotal = _summary_amount(lines, r'(?:subtotal|sub\s*total|total\s*before\s*tax)', currency)
    discount = _summary_amount(lines, r'(?:item\s*discounts?|less\s*discount|total\s*discounts?|discount|diskon)', currency, absolute=True)
    taxable_amount = _summary_amount(lines, r'(?:taxable\s*amount|taxable\s*value)', currency)
    tax = _summary_amount(lines, r'(?:gst\s*total|vat\s*total|total\s*tax|tax\s*total|tax\s*amount|total\s*gst|total\s*vat)', currency)
    # Generic single-line labels are a fallback; avoid labels such as Tax Registration No.
    if tax is None:
        tax = _summary_amount(lines, r'(?:tax|vat|gst|ppn|pajak)', currency)
    shipping = _summary_amount(lines, r'(?:shipping|freight|delivery\s*charges?)', currency)
    other_charges = _summary_amount(lines, r'(?:other\s*charges?|additional\s*charges?)', currency)
    rounding = _summary_amount(lines, r'(?:rounding|round\s*off)', currency)
    total = _summary_amount(lines, r'(?:po\s*total|purchase\s*order\s*total|amount\s*due|grand\s*total|invoice\s*total|total\s*(?:amount|due|payable|invoice|bill|tagihan|pembayaran)?)', currency)

    payment_terms = _value_after_label(lines, r'(?:payment\s*terms|terms\s*of\s*payment|syarat\s*pembayaran)')
    payment_reference = _value_after_label(lines, r'(?:payment\s*reference|remittance\s*reference|payment\s*ref(?:erence)?)')

    items = _extract_vertical_items(lines, currency)
    # Retain compatibility with the app's original synthetic text format.
    if not items:
        line_pattern = re.compile(
            r'ITEM\s*:\s*(.*?)\s*\|\s*(?:QTY|JUMLAH)\s*:\s*([0-9.,]+)\s*\|\s*(?:UNIT|SATUAN)\s*:\s*([^|\n]+)\s*\|\s*(?:UNIT\s*PRICE|HARGA(?:\s*SATUAN)?)\s*:\s*' + _AMOUNT_PREFIX + r'([0-9.,]+)\s*\|\s*(?:TAX|PAJAK)\s*:\s*([0-9.,]+)%?\s*\|\s*(?:TOTAL|TOTAL\s*ITEM)\s*:\s*' + _AMOUNT_PREFIX + r'([0-9.,]+)',
            re.I,
        )
        for match in line_pattern.finditer(text):
            quantity = parse_number(match.group(2))
            unit_price = parse_amount(match.group(4), currency)
            tax_rate = parse_number(match.group(5))
            line_total = parse_amount(match.group(6), currency)
            base = quantity * unit_price if quantity is not None and unit_price is not None else None
            tax_decimals = 0 if currency in {'IDR', 'JPY', 'KRW', 'VND', 'CLP'} else 2
            item_tax = round(base * tax_rate / 100, tax_decimals) if base is not None and tax_rate is not None else None
            items.append({
                'item_name': match.group(1).strip(), 'description': None, 'sku': None,
                'quantity': quantity, 'unit': match.group(3).strip(),
                'unit_price': unit_price, 'tax_rate': tax_rate,
                'item_tax': item_tax, 'line_total': line_total, 'item_discount': None,
                'item_discount_source': 'NOT_PRESENT',
            })

    # Dataset v4 and many supplier formats put document numbers under the title,
    # labels on their own line, and both party headings before party data.
    is_po = (document_type or '').lower() in {'po', 'purchase_order', 'purchase order'}
    if document_type is None:
        is_po = bool(re.search(r'(?im)^\s*PURCHASE ORDER\s*$', text)) and not bool(re.search(r'(?im)^\s*(?:COMMERCIAL )?INVOICE\s*$', text))
    if is_po:
        po_no = po_no or _header_identifier(lines, r'PURCHASE ORDER')
    else:
        invoice_no = invoice_no or _header_identifier(lines, r'(?:COMMERCIAL INVOICE|TAX INVOICE|INVOICE)')
        po_no = po_no or _value_after_label(lines, r'(?:PO REFERENCE|PURCHASE ORDER REFERENCE|PURCHASE ORDER REF|PO REF)')

    currency, currency_confidence = detect_currency(text)
    parties = _extract_party_records(lines)
    combined_party_headers = any(re.fullmatch(
        r'(?:SUPPLIER\s*/\s*FROM|CUSTOMER\s*/\s*BILL TO|BUYER\s*/\s*PURCHASING ENTITY|SUPPLIER\s*/\s*VENDOR)',
        line, re.I) for line in lines)
    if len(parties) >= 2:
        if is_po:
            robust_buyer, robust_vendor = parties[0], parties[1]
        else:
            robust_vendor, robust_buyer = parties[0], parties[1]
        # In side-by-side table layouts, the headings may precede both data blocks.
        # Use tax-anchored party records for all identity fields. For conventional
        # sequential blocks, preserve the more precise section parser output.
        if combined_party_headers:
            vendor_name = robust_vendor.get('name') or vendor_name
            address = robust_vendor.get('address') or address
            vendor_tax_id = robust_vendor.get('tax_id') or vendor_tax_id
            buyer_name = robust_buyer.get('name') or buyer_name
            buyer_address = robust_buyer.get('address') or buyer_address
            buyer_tax_id = robust_buyer.get('tax_id') or buyer_tax_id
        else:
            vendor_name = vendor_name or robust_vendor.get('name')
            address = address or robust_vendor.get('address')
            vendor_tax_id = vendor_tax_id or robust_vendor.get('tax_id')
            buyer_name = buyer_name or robust_buyer.get('name')
            buyer_address = buyer_address or robust_buyer.get('address')
            buyer_tax_id = buyer_tax_id or robust_buyer.get('tax_id')
    elif parties:
        # If only one party is identifiable, assign it only when role evidence is adequate.
        if is_po and not buyer_name:
            buyer_name = parties[0].get('name') or buyer_name
            buyer_address = parties[0].get('address') or buyer_address
            buyer_tax_id = parties[0].get('tax_id') or buyer_tax_id
        elif not is_po and not vendor_name:
            vendor_name = parties[0].get('name') or vendor_name
            address = parties[0].get('address') or address
            vendor_tax_id = parties[0].get('tax_id') or vendor_tax_id

    issue_labels = (r'(?:po\s*date|purchase\s*order\s*date|date\s*of\s*po)' if is_po else r'(?:invoice\s*date|date\s*of\s*(?:issue|invoice)|issue\s*date|tanggal\s*invoice|tanggal\s*terbit)')
    issue_date = _document_date(text, lines, issue_labels, currency)
    if not issue_date and not is_po:
        issue_date = _normalize_date(issue_raw)
    due_date = _document_date(text, lines, r'(?:payment\s*due(?:\s*date)?|due\s*date|payment\s*deadline|jatuh\s*tempo)', currency) or _normalize_date(due_raw)

    fields = {
        'invoice_number': _clean_identifier(invoice_no),
        'invoice_receive_date': received_at,
        'invoice_issue_date': issue_date,
        'vendor_company_name': (vendor_name or '').strip() or None,
        'vendor_address': (address or '').strip() or None,
        'vendor_tax_id': _clean_identifier(vendor_tax_id),
        'buyer_company_name': (buyer_name or '').strip() or None,
        'buyer_address': (buyer_address or '').strip() or None,
        'buyer_tax_id': _clean_identifier(buyer_tax_id),
        'purchase_order_number': _clean_identifier(po_no),
        'payment_terms': payment_terms,
        'payment_reference': payment_reference,
        'due_date': due_date,
        'currency': currency,
        'currency_confidence': currency_confidence,
        'bank_name': bank,
        'bank_account_number': re.sub(r'\D', '', account or '') or None,
        'bank_account_holder_name': holder,
        'subtotal': subtotal,
        'discount': discount,
        'taxable_amount': taxable_amount,
        'tax': tax,
        'shipping': shipping,
        'other_charges': other_charges,
        'rounding': rounding,
        'total': total,
        'items': items,
        'source_filename': original_filename,
    }
    fields['document_type'] = 'purchase_order' if is_po else 'invoice'
    fields['currency_source'] = 'EXPLICIT_DOCUMENT_EVIDENCE' if currency_confidence == 'HIGH' else ('CONFIGURED_USD_POLICY' if currency_confidence == 'POLICY' else 'CONFIGURED_DEFAULT_UNCONFIRMED')
    fields['bank_account_holder_name_source'] = 'EXPLICIT_DOCUMENT_VALUE' if fields.get('bank_account_holder_name') else 'MISSING'
    return fields, items

def extract_document(path: Path, original_filename: str, received_at: str, document_type: str | None = None) -> tuple[str, dict[str, Any], list[dict[str, Any]], str]:
    suffix = path.suffix.lower()
    mime = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}.get(
        suffix, mimetypes.guess_type(original_filename)[0] or 'application/octet-stream'
    )
    text = ''
    if suffix == '.pdf':
        import fitz
        from PIL import Image

        document = fitz.open(path)
        pages = []
        try:
            for page in document:
                page_text = page.get_text('text').strip()
                # A PDF can have a sparse or broken text layer even when it contains
                # enough characters to pass a simple 20-character check. Use OCR when
                # the extracted text does not resemble a usable text page.
                if not _has_usable_pdf_text(page_text):
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
                    image = Image.frombytes('RGB', [pixmap.width, pixmap.height], pixmap.samples)
                    try:
                        ocr_text = _ocr_pil_image(image)
                    except Exception:
                        ocr_text = ''
                    if len(ocr_text.strip()) > len(page_text.strip()):
                        page_text = ocr_text
                pages.append(page_text)
        finally:
            document.close()
        text = '\n'.join(pages)
    elif suffix in {'.png', '.jpg', '.jpeg'}:
        from PIL import Image, ImageOps
        with Image.open(path) as source_image:
            image = ImageOps.exif_transpose(source_image).convert('RGB')
            text = _ocr_pil_image(image)
    else:
        raise ValueError('Unsupported document format. Use PDF, PNG, or JPEG.')
    if not text.strip():
        raise ValueError('Text could not be read. Ensure the document is clear; OCR results require personnel review.')
    fields, items = extract_fields(text, received_at, original_filename, document_type=document_type)
    return text, fields, items, mime
