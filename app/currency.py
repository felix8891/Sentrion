"""Currency detection, parsing, and presentation helpers for Sentrion."""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import DEFAULT_CURRENCY

_ZERO_DECIMAL_CURRENCIES = {"IDR", "JPY", "KRW", "VND", "CLP"}
_KNOWN_CODES = ("USD", "IDR", "EUR", "GBP", "JPY", "KRW", "CAD", "AUD", "SGD", "CNY", "INR", "MYR", "THB", "PHP", "VND", "CHF", "NZD", "HKD", "AED", "SAR")


def detect_currency(text: str | None, default: str | None = None) -> tuple[str, str]:
    """Return (currency code, evidence grade).

    HIGH means document evidence is explicit. POLICY means the document has an
    unqualified dollar sign and the configured application policy resolves it to
    the configured default (USD by default). LOW means no reliable evidence exists.
    """
    source = text or ""
    fallback = (default or DEFAULT_CURRENCY or "USD").strip().upper()
    if not (len(fallback) == 3 and fallback.isalpha()):
        fallback = "USD"

    # A labelled ISO code has the strongest evidence, including a value on the next line.
    m = re.search(r"\bcurrency\s*[:=\-]?\s*([A-Z]{3})\b", source, re.I)
    if m and m.group(1).upper() in _KNOWN_CODES:
        return m.group(1).upper(), "HIGH"

    # Explicit currency codes and unambiguous symbols/tokens.
    checks = (
        (r"\bIDR\b|\bRupiah(?:\s+Indonesia)?\b|\bRp\.?\s*\d", "IDR"),
        (r"\bUSD\b|\bUS\$", "USD"),
        (r"\bEUR\b|€", "EUR"),
        (r"\bGBP\b|£", "GBP"),
        (r"\bJPY\b|\bYen\b|¥|￥", "JPY"),
        (r"\bCAD\b|C\$", "CAD"),
        (r"\bAUD\b|A\$", "AUD"),
        (r"\bSGD\b|S\$", "SGD"),
        (r"\bMYR\b|\bRM\s*\d", "MYR"),
        (r"\bTHB\b|฿", "THB"),
        (r"\bCNY\b|\bRMB\b|￥", "CNY"),
        (r"\bKRW\b|₩", "KRW"),
        (r"\bINR\b|₹", "INR"),
        (r"\bCHF\b", "CHF"),
        (r"\bNZD\b", "NZD"),
        (r"\bHKD\b", "HKD"),
        (r"\bAED\b", "AED"),
        (r"\bSAR\b", "SAR"),
        (r"\bPHP\b", "PHP"),
        (r"\bVND\b", "VND"),
    )
    for pattern, code in checks:
        if re.search(pattern, source, re.I):
            return code, "HIGH"

    # An unqualified $ is intentionally mapped through this app's configured default.
    # This project policy takes precedence over supplier-country context because a vendor
    # may invoice in USD even when its address is in Thailand or another country.
    if "$" in source:
        return fallback, "POLICY"

    # Some synthetic Thai documents have a font-encoding defect: the baht symbol is
    # extracted as a capital I. Use Thai location/tax evidence only if no currency code
    # or recognizable symbol was found above.
    if re.search(r'\bThailand\b|\bTH[- ]\d{2,}', source, re.I):
        return 'THB', 'HIGH'
    return fallback, "LOW"


def parse_money_amount(value: Any, currency: str | None = None) -> float | None:
    """Parse monetary text without silently converting the amount or currency."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.upper() in {"-", "—", "N/A", "NA", "NONE", "NULL"}:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("() ")
    code = (currency or DEFAULT_CURRENCY).upper()
    # Remove ISO currency codes and common unambiguous currency tokens/symbols.
    s = re.sub(r"\b(?:USD|IDR|EUR|GBP|JPY|KRW|CAD|AUD|SGD|CNY|INR|MYR|THB|PHP|VND|CHF|NZD|HKD|AED|SAR)\b", "", s, flags=re.I)
    s = re.sub(r"(?i)\b(?:US|CA|AU|S)\$", "", s)
    s = re.sub(r"[₹$€£¥￥₩]|\bRp\.?", "", s, flags=re.I)
    s = re.sub(r"[^0-9,.-]", "", s).strip()
    if not s or not re.search(r"\d", s):
        return None
    if s.count("-") > 1 or ("-" in s and not s.startswith("-")):
        return None
    if s.count(",") and s.count("."):
        # The last separator is decimal only if it has one or two trailing digits.
        last_comma, last_dot = s.rfind(","), s.rfind(".")
        last = max(last_comma, last_dot)
        fraction_len = len(s) - last - 1
        if fraction_len in (1, 2):
            s = re.sub(r"[,.]", "", s[:last]) + "." + s[last + 1:]
        else:
            s = re.sub(r"[,.]", "", s)
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        pieces = s.split(sep)
        trailing = len(pieces[-1])
        if trailing == 3 or len(pieces) > 2:
            s = "".join(pieces)
        else:
            s = "".join(pieces[:-1]) + "." + pieces[-1]
    try:
        amount = Decimal(s)
        if negative:
            amount = -abs(amount)
        return float(amount)
    except (InvalidOperation, ValueError):
        return None


def format_currency(value: Any, currency: str | None = None) -> str:
    """Format an amount with an explicit currency code, preserving numeric value."""
    if value is None or value == "":
        return "—"
    code = (currency or DEFAULT_CURRENCY or "USD").strip().upper()
    if not (len(code) == 3 and code.isalpha()):
        code = "UNKNOWN"
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            amount = float(Decimal(str(value)))
        except (InvalidOperation, ValueError):
            return "—"
    else:
        amount = parse_money_amount(value, code)
        if amount is None:
            return "—"
    if not math.isfinite(amount):
        return "—"
    decimals = 0 if code in _ZERO_DECIMAL_CURRENCIES else 2
    amount_text = f"{amount:,.{decimals}f}"
    return f"{code} {amount_text}"
