"""
normalize.py - Data Normalization and Data Quality Audit Layer

Tailored for real monday.com boards:
  - Board 1: Deals (Deal_funnel_Data)
  - Board 2: Work Orders (Work_Order_Tracker_Data)

Key data fixes & normalization rules:
  - English-only month parsing (Jan, Feb, Mar ... Dec) — non-English logic removed.
  - Deals: filters stray duplicate header-row values ("Deal Status") as garbage.
  - Deals: blank Close Date (A) is expected for open deals (not an error).
  - Deals: Deal Stage recognized as ordered funnel stages.
  - Deals: Closure Probability (~75% blank) tracked accurately.
  - Deals & WO: Sector/service & Sector normalized into canonical names for joins.
  - Work Orders: Collection status is 100% empty across all rows — flagged as "not currently tracked".
  - Work Orders: Billing Status case-normalized and typo-corrected ("BIlled" -> "Billed").
  - Work Orders: Invoice Status visit-numbered values ("Billed- Visit 7", "Billed- Visit 3") bucketed to "Partially Billed".
  - Work Orders: WO Status (billed) blank (~42%) treated as "Unknown" (never coerced to Open or Closed).
  - Work Orders: Month-only fields vs full date fields parsed separately.
  - Work Orders: All (Masked) Rupee fields treated as real numeric values.
  - Comprehensive per-column data_quality_report() with null counts, null %, normalization actions, and inline caveats.
"""

import re
import math
import json
import logging
from datetime import datetime, date
from typing import Dict, Any, List, Optional, Tuple, Set

logger = logging.getLogger("normalize")

# ---------------------------------------------------------------------------
# English-only month abbreviation / name mapping
# ---------------------------------------------------------------------------
ENGLISH_MONTH_MAP = {
    "jan": "01", "january": "01",
    "feb": "02", "february": "02",
    "mar": "03", "march": "03",
    "apr": "04", "april": "04",
    "may": "05",
    "jun": "06", "june": "06",
    "jul": "07", "july": "07",
    "aug": "08", "august": "08",
    "sep": "09", "sept": "09", "september": "09",
    "oct": "10", "october": "10",
    "nov": "11", "november": "11",
    "dec": "12", "december": "12",
}

# ---------------------------------------------------------------------------
# Deals Board Constants
# ---------------------------------------------------------------------------
VALID_DEAL_STATUSES = {"Won", "Dead", "Open", "On Hold"}
GARBAGE_DEAL_STATUS_VALUES = {"deal status"}  # Stray header-row values from bad copy/paste

# Funnel stage ordering
DEAL_STAGE_ORDER = [
    "A. Lead Generated",
    "B. Lead Qualified",
    "C. Initial Discussion",
    "D. Requirement Gathering",
    "E. Proposal Submitted",
    "F. Negotiation",
    "G. Verbal Agreement",
    "H. Contract Sent",
    "I. Won / Closed",
    "Project Completed",
    "O. Not Relevant at all"
]

# ---------------------------------------------------------------------------
# Work Orders Status Normalization Maps
# ---------------------------------------------------------------------------
BILLING_STATUS_NORM = {
    "billed": "Billed",
    "not billed": "Not Billed",
    "not billed yet": "Not Billed",
    "unbilled": "Not Billed",
    "partially billed": "Partially Billed",
    "partial": "Partially Billed",
    "stuck": "Stuck",
}

INVOICE_STATUS_NORM = {
    "fully billed": "Fully Billed",
    "partially billed": "Partially Billed",
    "not billed yet": "Not Billed",
    "not billed": "Not Billed",
    "stuck": "Stuck",
}
INVOICE_VISIT_PATTERN = re.compile(r"billed[-\s]+visit\s*\d+", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Canonical Sector Mapping (for cross-board joins)
# ---------------------------------------------------------------------------
SECTOR_CANONICAL = {
    "mining": "Mining",
    "renewables": "Renewables",
    "renewable": "Renewables",
    "railways": "Railways",
    "railway": "Railways",
    "powerline": "Powerline",
    "power line": "Powerline",
    "construction": "Construction",
    "others": "Others",
    "other": "Others",
    "agriculture": "Agriculture",
    "agri": "Agriculture",
    "defense": "Defense & Security",
    "security": "Defense & Security",
    "logistics": "Logistics",
    "transport": "Logistics",
    "government": "Government",
    "public sector": "Government",
    "energy": "Renewables",
    "infra": "Construction",
    "infrastructure": "Construction",
}


# ===========================================================================
# Basic Helpers
# ===========================================================================

def clean_text_field(val: Any) -> Optional[str]:
    """Standardizes string values; returns None for null-like or blank inputs."""
    if val is None:
        return None
    text = str(val).strip()
    if not text or text.lower() in ("null", "none", "n/a", "na", "-", ""):
        return None
    return text


def normalize_numeric(val: Any) -> Optional[float]:
    """
    Parses currency strings, formatted numbers, or multipliers into float.
    Treats masked monetary fields as real numeric values.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if not math.isnan(val) else None

    text = str(val).strip()
    if not text or text.lower() in ("null", "none", "n/a", "na", "-", ""):
        return None

    # Handle monday.com numbers column wrapped in JSON: {"value": "..."}
    if text.startswith("{") and "value" in text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and "value" in parsed:
                text = str(parsed["value"])
        except Exception:
            pass

    # Multiplier handling: 50k, 1.5M, 2B, etc.
    mult_match = re.match(r'^[\$€£₹\s]*([0-9]+(?:\.[0-9]+)?)\s*([kKmMbB])\s*$', text)
    if mult_match:
        num = float(mult_match.group(1))
        unit = mult_match.group(2).lower()
        multipliers = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
        return num * multipliers[unit]

    # Clean commas and currency symbols
    cleaned = re.sub(r'[^\d.-]', '', text.replace(',', ''))
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


# ===========================================================================
# Date Parsing — English Only
# ===========================================================================

def parse_full_date(val: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    Parses a full calendar date into (iso_date: YYYY-MM-DD, month_str: YYYY-MM).
    Supports English month names/abbreviations only (Jan, Feb, Mar, etc.).
    Returns (None, None) for empty/unparseable dates.
    """
    if val is None:
        return None, None

    text = str(val).strip()
    if not text or text.lower() in ("null", "none", "n/a", "na", "-", ""):
        return None, None

    # monday.com JSON date object: {"date": "YYYY-MM-DD"}
    if text.startswith("{") and "date" in text:
        try:
            parsed_json = json.loads(text)
            if isinstance(parsed_json, dict) and "date" in parsed_json:
                text = str(parsed_json["date"])
        except Exception:
            pass

    clean = text.lower().strip()
    # Strip ordinal suffixes: 1st, 2nd, 3rd, 4th -> 1, 2, 3, 4
    clean = re.sub(r'(\d+)(st|nd|rd|th)\b', r'\1', clean)
    clean = re.sub(r'\bof\b', ' ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()

    # 1. Direct ISO match: YYYY-MM-DD
    iso_match = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', clean)
    if iso_match:
        y, m, d = int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3))
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}", f"{y:04d}-{m:02d}"

    # 2. English month names replacement
    replaced = clean
    found_month_num = None
    for token, m_num in ENGLISH_MONTH_MAP.items():
        if re.search(r'\b' + re.escape(token) + r'\b', replaced):
            found_month_num = m_num
            replaced = re.sub(r'\b' + re.escape(token) + r'\b', f"M{m_num}", replaced)
            break

    if found_month_num:
        # Day Month Year: "15 M03 2026" / "15-M03-2026"
        dmatch = re.match(r'^(\d{1,2})[\s,/-]+M(\d{2})[\s,/-]+(\d{2,4})$', replaced)
        if dmatch:
            d, m, y = int(dmatch.group(1)), int(dmatch.group(2)), int(dmatch.group(3))
            if y < 100:
                y += 2000
            if 1 <= m <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{m:02d}-{d:02d}", f"{y:04d}-{m:02d}"

        # Month Day Year: "M03 15 2026"
        mdmatch = re.match(r'^M(\d{2})[\s,/-]+(\d{1,2})[\s,/-]+(\d{2,4})$', replaced)
        if mdmatch:
            m, d, y = int(mdmatch.group(1)), int(mdmatch.group(2)), int(mdmatch.group(3))
            if y < 100:
                y += 2000
            if 1 <= m <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{m:02d}-{d:02d}", f"{y:04d}-{m:02d}"

        # Month Year only: "M03 2026"
        my_match = re.match(r'^M(\d{2})[\s,/_-]+(\d{2,4})$', replaced) or re.match(r'^(\d{2,4})[\s,/_-]+M(\d{2})$', replaced)
        if my_match:
            g1, g2 = my_match.group(1), my_match.group(2)
            if g1.startswith("M"):
                m_val, y_val = int(g1[1:]), int(g2)
            else:
                m_val, y_val = int(g2[1:]) if g2.startswith("M") else int(g2), int(g1)
            if y_val < 100:
                y_val += 2000
            if 1 <= m_val <= 12:
                return None, f"{y_val:04d}-{m_val:02d}"

    # 3. Numeric formats (DD/MM/YYYY or MM/DD/YYYY)
    slash_match = re.match(r'^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})$', clean)
    if slash_match:
        p1, p2, p3 = int(slash_match.group(1)), int(slash_match.group(2)), int(slash_match.group(3))
        y = p3 + 2000 if p3 < 100 else p3
        if p1 > 12:
            d, m = p1, p2
        elif p2 > 12:
            m, d = p1, p2
        else:
            d, m = p1, p2  # Default DD/MM/YYYY
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}", f"{y:04d}-{m:02d}"

    # 4. YYYY/MM/DD
    ymd_match = re.match(r'^(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})$', clean)
    if ymd_match:
        y, m, d = int(ymd_match.group(1)), int(ymd_match.group(2)), int(ymd_match.group(3))
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}", f"{y:04d}-{m:02d}"

    # Fallback to dateutil with dayfirst=True
    try:
        from dateutil import parser as dateutil_parser
        dt = dateutil_parser.parse(clean, dayfirst=True, fuzzy=False)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m")
    except Exception:
        pass

    if re.search(r'\d', clean):
        try:
            from dateutil import parser as dateutil_parser
            dt = dateutil_parser.parse(clean, dayfirst=True, fuzzy=True)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m")
        except Exception:
            pass

    return None, None


def parse_month_only_field(val: Any) -> Optional[str]:
    """
    Parses month-only fields (e.g., "Jan", "Feb 2026", "2026-03", "Aug").
    Returns 'YYYY-MM' string or None.
    English month abbreviations only.
    """
    if val is None:
        return None
    text = str(val).strip()
    if not text or text.lower() in ("null", "none", "n/a", "na", "-", ""):
        return None

    clean = text.lower().strip()

    # YYYY-MM
    ym_match = re.match(r'^(\d{4})-(\d{1,2})$', clean)
    if ym_match:
        y, m = int(ym_match.group(1)), int(ym_match.group(2))
        if 1 <= m <= 12:
            return f"{y:04d}-{m:02d}"

    # English month name + optional year
    for token, m_num in ENGLISH_MONTH_MAP.items():
        if re.search(r'\b' + re.escape(token) + r'\b', clean):
            year_match = re.search(r'\b(\d{4})\b', clean)
            if year_match:
                y = int(year_match.group(1))
            else:
                y2_match = re.search(r'\b(\d{2})\b', clean)
                if y2_match and not re.search(r'\b' + y2_match.group(1) + r'\b', token):
                    y = int(y2_match.group(1)) + 2000
                else:
                    y = datetime.now().year
            return f"{y:04d}-{m_num}"

    # MM/YYYY
    m_y_match = re.match(r'^(\d{1,2})[/\-](\d{4})$', clean)
    if m_y_match:
        m, y = int(m_y_match.group(1)), int(m_y_match.group(2))
        if 1 <= m <= 12:
            return f"{y:04d}-{m:02d}"

    # YYYY/MM
    y_m_match = re.match(r'^(\d{4})[/\-](\d{1,2})$', clean)
    if y_m_match:
        y, m = int(y_m_match.group(1)), int(y_m_match.group(2))
        if 1 <= m <= 12:
            return f"{y:04d}-{m:02d}"

    return None


# ===========================================================================
# Categorical Normalizers
# ===========================================================================

def normalize_deal_status(val: Any) -> Optional[str]:
    """
    Normalizes Deal Status.
    Returns None for duplicate header-row values ("Deal Status") to signal garbage rows.
    """
    if val is None:
        return None
    text = str(val).strip()
    if not text or text.lower() in ("null", "none", "n/a", "na", "-", ""):
        return None
    low = text.lower()
    if low in GARBAGE_DEAL_STATUS_VALUES:
        return None  # Signal to filter this row

    for canonical in VALID_DEAL_STATUSES:
        if low == canonical.lower():
            return canonical

    if "on hold" in low or "hold" in low:
        return "On Hold"
    if "won" in low:
        return "Won"
    if "dead" in low or "lost" in low:
        return "Dead"
    if "open" in low or "active" in low:
        return "Open"

    return text.title()


def normalize_billing_status(val: Any) -> Optional[str]:
    """
    Case-normalizes and typo-corrects Billing Status (e.g., 'BIlled' -> 'Billed').
    """
    if val is None:
        return None
    text = str(val).strip()
    if not text or text.lower() in ("null", "none", "n/a", "na", "-", ""):
        return None
    low = text.lower()
    return BILLING_STATUS_NORM.get(low, text.title())


def normalize_invoice_status(val: Any) -> Tuple[Optional[str], bool]:
    """
    Normalizes Invoice Status, bucketing visit-numbered items ("Billed- Visit 7")
    into "Partially Billed".
    Returns (normalized_status, was_bucketed_flag).
    """
    if val is None:
        return None, False
    text = str(val).strip()
    if not text or text.lower() in ("null", "none", "n/a", "na", "-", ""):
        return None, False

    if INVOICE_VISIT_PATTERN.match(text.strip()):
        return "Partially Billed", True

    low = text.lower()
    if low in INVOICE_STATUS_NORM:
        return INVOICE_STATUS_NORM[low], False

    if "fully" in low and "billed" in low:
        return "Fully Billed", False
    if "partial" in low:
        return "Partially Billed", False
    if "stuck" in low:
        return "Stuck", False
    if "not" in low and "billed" in low:
        return "Not Billed", False

    return text.strip(), False


def normalize_wo_status(val: Any) -> str:
    """
    Normalizes WO Status (billed).
    Blank (~42% of rows) is treated as "Unknown" — never coerced to Open or Closed.
    """
    if val is None:
        return "Unknown"
    text = str(val).strip()
    if not text or text.lower() in ("null", "none", "n/a", "na", "-", ""):
        return "Unknown"
    low = text.lower()
    if "closed" in low:
        return "Closed"
    if "open" in low:
        return "Open"
    return text.title()


def normalize_sector(val: Any) -> str:
    """
    Standardizes sector values across both boards (Deals: 'Sector/service', WO: 'Sector').
    Returns 'Unspecified' for blank entries.
    """
    if val is None:
        return "Unspecified"
    text = str(val).strip()
    if not text or text.lower() in ("null", "none", "n/a", "na", "-", ""):
        return "Unspecified"
    low = text.lower()
    for key, canonical in SECTOR_CANONICAL.items():
        if key in low:
            return canonical
    return text.title()


# ===========================================================================
# Data Quality Reporting Architecture
# ===========================================================================

class ColumnQuality:
    """Tracks null statistics and normalization actions for a single column."""

    def __init__(self, column_name: str, total_records: int):
        self.column_name = column_name
        self.total_records = total_records
        self.null_count: int = 0
        self.norm_actions: List[str] = []

    def record_null(self):
        self.null_count += 1

    def add_norm_action(self, description: str):
        if description not in self.norm_actions:
            self.norm_actions.append(description)

    @property
    def null_pct(self) -> float:
        if self.total_records == 0:
            return 0.0
        return round(self.null_count / self.total_records * 100, 1)

    @property
    def has_high_nulls(self) -> bool:
        return self.null_pct > 10.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "column": self.column_name,
            "null_count": self.null_count,
            "null_pct": self.null_pct,
            "high_null_flag": self.has_high_nulls,
            "normalization_actions": self.norm_actions,
        }


class DataQualityReport:
    """
    Aggregates per-column data quality statistics, transformations, and inline caveats.
    """

    def __init__(self, board_name: str, total_records: int):
        self.board_name = board_name
        self.total_records = total_records
        self._columns: Dict[str, ColumnQuality] = {}
        self.board_caveats: List[str] = []
        self._unreliable_fields: Dict[str, str] = {}

    def track(self, column_name: str) -> ColumnQuality:
        if column_name not in self._columns:
            self._columns[column_name] = ColumnQuality(column_name, self.total_records)
        return self._columns[column_name]

    def flag_unreliable(self, field_name: str, reason: str):
        self._unreliable_fields[field_name] = reason
        if reason not in self.board_caveats:
            self.board_caveats.append(reason)

    def get_inline_caveat(self, field_name: str) -> Optional[str]:
        """Returns contextual caveat string if field has >10% nulls or is unreliable."""
        if field_name in self._unreliable_fields:
            return f"⚠️ *{field_name}*: {self._unreliable_fields[field_name]}"
        col = self._columns.get(field_name)
        if col and col.has_high_nulls:
            return (
                f"⚠️ *{field_name}*: {col.null_count}/{self.total_records} "
                f"({col.null_pct}%) rows are blank — calculations reflect available records only."
            )
        return None

    def finalize(self):
        for col in self._columns.values():
            if col.has_high_nulls and col.column_name not in self._unreliable_fields:
                msg = (
                    f"'{col.column_name}' is blank in {col.null_count}/{self.total_records} "
                    f"({col.null_pct}%) rows on '{self.board_name}'."
                )
                if msg not in self.board_caveats:
                    self.board_caveats.append(msg)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "board_name": self.board_name,
            "total_records": self.total_records,
            "columns": [c.to_dict() for c in self._columns.values()],
            "unreliable_fields": self._unreliable_fields,
            "board_caveats": self.board_caveats,
        }


# ===========================================================================
# Board Normalization Implementations
# ===========================================================================

def _get_column_text(vals_by_id: Dict[str, Any], col_id: Optional[str]) -> Optional[str]:
    """Helper to extract text from a monday.com column value."""
    if not col_id or col_id not in vals_by_id:
        return None
    cv = vals_by_id[col_id]
    if isinstance(cv, dict):
        text = cv.get("text")
        if text is not None:
            return str(text).strip() or None
        val = cv.get("value")
        if val is not None:
            return str(val).strip() or None
    return None


def normalize_deals_data(
    board_metadata: Dict[str, Any],
    raw_items: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], DataQualityReport]:
    """
    Normalizes Deals (Deal_funnel_Data) items using exact column names.
    Filters stray duplicated header rows.
    """
    total_raw = len(raw_items)
    report = DataQualityReport(
        board_name=board_metadata.get("name", "Deals"),
        total_records=total_raw
    )

    # Build title -> column_id map from live schema
    columns = board_metadata.get("columns", [])
    col_map = {c.get("title", "").strip(): c.get("id", "") for c in columns if c.get("title") and c.get("id")}

    # Track known columns
    deals_columns = [
        "Deal Name", "Owner code", "Client Code", "Deal Status",
        "Close Date (A)", "Closure Probability", "Masked Deal value",
        "Tentative Close Date", "Deal Stage", "Product deal",
        "Sector/service", "Created Date"
    ]
    for col in deals_columns:
        report.track(col)

    report.track("Closure Probability").add_norm_action(
        "Expected ~75% blank (only populated for open in-progress deals)"
    )
    report.track("Close Date (A)").add_norm_action(
        "Expected blank for open/unclosed deals (not treated as error)"
    )

    normalized_items: List[Dict[str, Any]] = []
    garbage_rows = 0

    for item in raw_items:
        vals_by_id = {cv.get("id"): cv for cv in item.get("column_values", [])}

        def get_field(col_name: str) -> Optional[str]:
            return _get_column_text(vals_by_id, col_map.get(col_name))

        # Check Deal Status & filter garbage header rows
        raw_status = get_field("Deal Status")
        if raw_status and raw_status.lower() in GARBAGE_DEAL_STATUS_VALUES:
            garbage_rows += 1
            continue

        norm_status = normalize_deal_status(raw_status)
        if norm_status is None:
            report.track("Deal Status").record_null()

        # Deal Name
        raw_deal_name = get_field("Deal Name") or clean_text_field(item.get("name"))
        if not raw_deal_name:
            report.track("Deal Name").record_null()
        deal_name = raw_deal_name or "Unnamed Deal"

        # Owner Code
        owner = get_field("Owner code") or "Unassigned"
        if owner == "Unassigned":
            report.track("Owner code").record_null()

        # Client Code
        client = get_field("Client Code")
        if not client:
            report.track("Client Code").record_null()

        # Masked Deal value
        raw_val = get_field("Masked Deal value")
        norm_val = normalize_numeric(raw_val)
        if norm_val is None:
            report.track("Masked Deal value").record_null()

        # Close Date (A) - Full date parsing
        raw_close = get_field("Close Date (A)")
        iso_close, month_close = parse_full_date(raw_close)
        if iso_close is None:
            report.track("Close Date (A)").record_null()

        # Tentative Close Date - Full date parsing
        raw_tentative = get_field("Tentative Close Date")
        iso_tentative, month_tentative = parse_full_date(raw_tentative)
        if iso_tentative is None:
            report.track("Tentative Close Date").record_null()

        # Closure Probability
        raw_prob = get_field("Closure Probability")
        norm_prob = normalize_numeric(raw_prob)
        if norm_prob is None:
            report.track("Closure Probability").record_null()

        # Deal Stage (Ordered Funnel)
        stage = get_field("Deal Stage")
        if not stage:
            report.track("Deal Stage").record_null()

        # Product deal
        product = get_field("Product deal")
        if not product:
            report.track("Product deal").record_null()

        # Sector/service
        raw_sector = get_field("Sector/service")
        norm_sector = normalize_sector(raw_sector)
        if norm_sector == "Unspecified":
            report.track("Sector/service").record_null()

        # Created Date
        raw_created = get_field("Created Date") or item.get("created_at")
        iso_created, _ = parse_full_date(raw_created)
        if not iso_created:
            report.track("Created Date").record_null()

        record = {
            "id": item.get("id"),
            "deal_name": deal_name,
            "owner_code": owner,
            "client_code": client,
            "deal_status": norm_status,
            "close_date": iso_close,
            "close_month": month_close,
            "closure_probability": norm_prob,
            "deal_value": norm_val,
            "tentative_close_date": iso_tentative,
            "tentative_close_month": month_tentative,
            "deal_stage": stage,
            "product_deal": product,
            "sector": norm_sector,
            "created_date": iso_created,
        }
        normalized_items.append(record)

    if garbage_rows > 0:
        report.track("Deal Status").add_norm_action(
            f"Filtered {garbage_rows} duplicate header row(s) ('Deal Status' in data)"
        )
        report.board_caveats.append(
            f"{garbage_rows} garbage row(s) removed (stray header row artifact)."
        )

    report.finalize()
    return normalized_items, report


def normalize_work_orders_data(
    board_metadata: Dict[str, Any],
    raw_items: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], DataQualityReport]:
    """
    Normalizes Work Orders (Work_Order_Tracker_Data) items using exact column names.
    """
    total_raw = len(raw_items)
    report = DataQualityReport(
        board_name=board_metadata.get("name", "Work Orders"),
        total_records=total_raw
    )

    # 100% empty field flag
    report.flag_unreliable(
        "Collection status",
        "Collection status is 100% empty across all rows — not currently tracked in operations."
    )

    columns = board_metadata.get("columns", [])
    # Defensive against leading blank headers
    col_map = {c.get("title", "").strip(): c.get("id", "") for c in columns if c.get("title") and c.get("title").strip() and c.get("id")}

    wo_columns = [
        "Deal name masked", "Customer Name Code", "Serial #", "Nature of Work",
        "Last executed month of recurring project", "Execution Status",
        "Data Delivery Date", "Date of PO/LOI", "Document Type", "Probable Start Date",
        "Probable End Date", "BD/KAM Personnel code", "Sector", "Type of Work",
        "Is any Skylark software platform part of the client deliverables in this deal?",
        "Last invoice date", "latest invoice no.",
        "Amount in Rupees (Excl of GST) (Masked)", "Amount in Rupees (Incl of GST) (Masked)",
        "Billed Value in Rupees (Excl of GST.) (Masked)", "Billed Value in Rupees (Incl of GST.) (Masked)",
        "Collected Amount in Rupees (Incl of GST.) (Masked)",
        "Amount to be billed in Rs. (Exl. of GST) (Masked)", "Amount to be billed in Rs. (Incl. of GST) (Masked)",
        "Amount Receivable (Masked)", "AR Priority account", "Quantity by Ops",
        "Quantities as per PO", "Quantity billed (till date)", "Balance in quantity",
        "Invoice Status", "Expected Billing Month", "Actual Billing Month",
        "Actual Collection Month", "WO Status (billed)", "Collection status",
        "Collection Date", "Billing Status"
    ]
    for col in wo_columns:
        report.track(col)

    report.track("WO Status (billed)").add_norm_action(
        "Blank values treated as 'Unknown' (~42% expected), never coerced"
    )
    report.track("Invoice Status").add_norm_action(
        "Visit-numbered entries ('Billed- Visit 7') bucketed to 'Partially Billed'"
    )
    report.track("Billing Status").add_norm_action(
        "Case-normalized & typo-corrected ('BIlled' -> 'Billed')"
    )

    normalized_items: List[Dict[str, Any]] = []
    bucketed_invoice_count = 0
    billing_corrected_count = 0

    full_date_cols = [
        "Data Delivery Date", "Date of PO/LOI", "Probable Start Date",
        "Probable End Date", "Last invoice date", "Collection Date"
    ]
    month_only_cols = [
        "Last executed month of recurring project",
        "Expected Billing Month", "Actual Billing Month", "Actual Collection Month"
    ]
    monetary_cols = [
        "Amount in Rupees (Excl of GST) (Masked)",
        "Amount in Rupees (Incl of GST) (Masked)",
        "Billed Value in Rupees (Excl of GST.) (Masked)",
        "Billed Value in Rupees (Incl of GST.) (Masked)",
        "Collected Amount in Rupees (Incl of GST.) (Masked)",
        "Amount to be billed in Rs. (Exl. of GST) (Masked)",
        "Amount to be billed in Rs. (Incl. of GST) (Masked)",
        "Amount Receivable (Masked)"
    ]

    for item in raw_items:
        vals_by_id = {cv.get("id"): cv for cv in item.get("column_values", [])}

        def get_field(col_name: str) -> Optional[str]:
            return _get_column_text(vals_by_id, col_map.get(col_name))

        # Identity & Metadata
        raw_deal_name = get_field("Deal name masked") or clean_text_field(item.get("name"))
        if not raw_deal_name:
            report.track("Deal name masked").record_null()
        deal_name = raw_deal_name or "Unnamed Work Order"

        customer = get_field("Customer Name Code")
        if not customer:
            report.track("Customer Name Code").record_null()

        serial = get_field("Serial #")
        if not serial:
            report.track("Serial #").record_null()

        nature = get_field("Nature of Work")
        if not nature:
            report.track("Nature of Work").record_null()

        type_of_work = get_field("Type of Work")
        if not type_of_work:
            report.track("Type of Work").record_null()

        # Sector
        raw_sector = get_field("Sector")
        norm_sector = normalize_sector(raw_sector)
        if norm_sector == "Unspecified":
            report.track("Sector").record_null()

        exec_status = get_field("Execution Status")
        if not exec_status:
            report.track("Execution Status").record_null()

        bd_kam = get_field("BD/KAM Personnel code")
        if not bd_kam:
            report.track("BD/KAM Personnel code").record_null()

        doc_type = get_field("Document Type")
        if not doc_type:
            report.track("Document Type").record_null()

        skylark_platform = get_field("Is any Skylark software platform part of the client deliverables in this deal?")
        if not skylark_platform:
            report.track("Is any Skylark software platform part of the client deliverables in this deal?").record_null()

        # Full Date Parsing
        parsed_dates = {}
        for dcol in full_date_cols:
            raw_d = get_field(dcol)
            iso_d, _ = parse_full_date(raw_d)
            parsed_dates[dcol] = iso_d
            if not iso_d:
                report.track(dcol).record_null()

        # Month-Only Parsing
        parsed_months = {}
        for mcol in month_only_cols:
            raw_m = get_field(mcol)
            norm_m = parse_month_only_field(raw_m)
            parsed_months[mcol] = norm_m
            if not norm_m:
                report.track(mcol).record_null()

        # Status Fields
        raw_inv = get_field("Invoice Status")
        norm_inv, was_bucketed = normalize_invoice_status(raw_inv)
        if not norm_inv:
            report.track("Invoice Status").record_null()
        if was_bucketed:
            bucketed_invoice_count += 1

        raw_wo_stat = get_field("WO Status (billed)")
        norm_wo_stat = normalize_wo_status(raw_wo_stat)
        if norm_wo_stat == "Unknown":
            report.track("WO Status (billed)").record_null()

        raw_bill = get_field("Billing Status")
        norm_bill = normalize_billing_status(raw_bill)
        if not norm_bill:
            report.track("Billing Status").record_null()
        elif raw_bill and norm_bill != raw_bill.strip().title():
            billing_corrected_count += 1

        # Collection status is always null
        report.track("Collection status").record_null()

        # Monetary fields (Masked Rupee values)
        parsed_monetary = {}
        for mcol in monetary_cols:
            raw_val = get_field(mcol)
            norm_val = normalize_numeric(raw_val)
            parsed_monetary[mcol] = norm_val
            if norm_val is None:
                report.track(mcol).record_null()

        # Invoicing / AR / Quantities
        latest_inv_no = get_field("latest invoice no.")
        if not latest_inv_no:
            report.track("latest invoice no.").record_null()

        ar_priority = get_field("AR Priority account")
        if not ar_priority:
            report.track("AR Priority account").record_null()

        qty_ops = normalize_numeric(get_field("Quantity by Ops"))
        qty_po = normalize_numeric(get_field("Quantities as per PO"))
        qty_billed = normalize_numeric(get_field("Quantity billed (till date)"))
        qty_bal = normalize_numeric(get_field("Balance in quantity"))
        if qty_ops is None: report.track("Quantity by Ops").record_null()
        if qty_po is None: report.track("Quantities as per PO").record_null()
        if qty_billed is None: report.track("Quantity billed (till date)").record_null()
        if qty_bal is None: report.track("Balance in quantity").record_null()

        record = {
            "id": item.get("id"),
            "deal_name_masked": deal_name,
            "customer_name_code": customer,
            "serial_num": serial,
            "nature_of_work": nature,
            "type_of_work": type_of_work,
            "sector": norm_sector,
            "execution_status": exec_status,
            "bd_kam_code": bd_kam,
            "document_type": doc_type,
            "skylark_platform": skylark_platform,
            # Full Dates
            "data_delivery_date": parsed_dates.get("Data Delivery Date"),
            "date_of_po_loi": parsed_dates.get("Date of PO/LOI"),
            "probable_start_date": parsed_dates.get("Probable Start Date"),
            "probable_end_date": parsed_dates.get("Probable End Date"),
            "last_invoice_date": parsed_dates.get("Last invoice date"),
            "collection_date": parsed_dates.get("Collection Date"),
            # Month-Only Fields
            "last_executed_month": parsed_months.get("Last executed month of recurring project"),
            "expected_billing_month": parsed_months.get("Expected Billing Month"),
            "actual_billing_month": parsed_months.get("Actual Billing Month"),
            "actual_collection_month": parsed_months.get("Actual Collection Month"),
            # Statuses
            "invoice_status": norm_inv,
            "wo_status_billed": norm_wo_stat,
            "billing_status": norm_bill,
            "collection_status": None,  # Always flagged as untracked
            # Monetary (Rupees Masked)
            "amount_excl_gst": parsed_monetary.get("Amount in Rupees (Excl of GST) (Masked)"),
            "amount_incl_gst": parsed_monetary.get("Amount in Rupees (Incl of GST) (Masked)"),
            "billed_excl_gst": parsed_monetary.get("Billed Value in Rupees (Excl of GST.) (Masked)"),
            "billed_incl_gst": parsed_monetary.get("Billed Value in Rupees (Incl of GST.) (Masked)"),
            "collected_incl_gst": parsed_monetary.get("Collected Amount in Rupees (Incl of GST.) (Masked)"),
            "to_be_billed_excl_gst": parsed_monetary.get("Amount to be billed in Rs. (Exl. of GST) (Masked)"),
            "to_be_billed_incl_gst": parsed_monetary.get("Amount to be billed in Rs. (Incl. of GST) (Masked)"),
            "amount_receivable": parsed_monetary.get("Amount Receivable (Masked)"),
            # Quantities & Others
            "qty_ops": qty_ops,
            "qty_po": qty_po,
            "qty_billed": qty_billed,
            "qty_balance": qty_bal,
            "ar_priority": ar_priority,
            "latest_invoice_no": latest_inv_no,
        }
        normalized_items.append(record)

    if bucketed_invoice_count > 0:
        report.track("Invoice Status").add_norm_action(
            f"Normalized {bucketed_invoice_count} visit-numbered entries to 'Partially Billed'"
        )
    if billing_corrected_count > 0:
        report.track("Billing Status").add_norm_action(
            f"Corrected {billing_corrected_count} casing variants to 'Billed'"
        )

    report.finalize()
    return normalized_items, report


# ===========================================================================
# Standalone data_quality_report()
# ===========================================================================

def data_quality_report(
    deals_meta: Dict[str, Any],
    deals_raw: List[Dict[str, Any]],
    wo_meta: Dict[str, Any],
    wo_raw: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Produces a consolidated data quality report across both boards.
    """
    _, deals_rep = normalize_deals_data(deals_meta, deals_raw)
    _, wo_rep = normalize_work_orders_data(wo_meta, wo_raw)

    return {
        "deals_board": deals_rep.to_dict(),
        "work_orders_board": wo_rep.to_dict(),
        "summary": {
            "deals_total_records": deals_rep.total_records,
            "wo_total_records": wo_rep.total_records,
            "deals_high_null_columns": [
                c["column"] for c in deals_rep.to_dict()["columns"] if c["high_null_flag"]
            ],
            "wo_high_null_columns": [
                c["column"] for c in wo_rep.to_dict()["columns"] if c["high_null_flag"]
            ],
            "unreliable_fields": {
                "work_orders": list(wo_rep._unreliable_fields.keys()),
                "deals": list(deals_rep._unreliable_fields.keys()),
            }
        }
    }
