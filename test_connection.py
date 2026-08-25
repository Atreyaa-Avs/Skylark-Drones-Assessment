"""
test_connection.py - Diagnostic CLI utility for verifying monday.com API & LLM configuration

Usage:
    python test_connection.py
"""

import os
import sys
from dotenv import load_dotenv
from tabulate import tabulate

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from monday_client import MondayClient, MondayAPIError
from normalize import (
    parse_full_date,
    parse_month_only_field,
    normalize_numeric,
    normalize_sector,
    normalize_deal_status,
    normalize_billing_status,
    normalize_invoice_status
)

load_dotenv()


def print_banner(title: str):
    print("\n" + "=" * 65)
    print(f"  {title}")
    print("=" * 65)


def run_diagnostics():
    print_banner("🔍 monday.com Founder BI Agent - Diagnostics")

    # 1. Check Environment Variables
    monday_api_key = os.getenv("MONDAY_API_KEY", "")
    wo_board_id = os.getenv("WORK_ORDERS_BOARD_ID", "")
    deals_board_id = os.getenv("DEALS_BOARD_ID", "")
    groq_key = os.getenv("GROQ_API_KEY", "")
    groq_key2 = os.getenv("GROQ_API_KEY2", "")
    groq_model = os.getenv("GROQ_MODEL", os.getenv("LLM_MODEL", "llama-3.3-70b-versatile"))

    env_rows = [
        ["MONDAY_API_KEY", "Set" if (monday_api_key and not monday_api_key.startswith("your_")) else "Missing / Placeholder"],
        ["WORK_ORDERS_BOARD_ID", wo_board_id if wo_board_id else "Not Set"],
        ["DEALS_BOARD_ID", deals_board_id if deals_board_id else "Not Set"],
        ["GROQ_API_KEY (Primary)", "Set" if (groq_key and not groq_key.startswith("your_")) else "Not Set"],
        ["GROQ_API_KEY2 (Fallback)", "Set" if (groq_key2 and not groq_key2.startswith("your_")) else "Not Set"],
        ["GROQ_MODEL", groq_model],
    ]
    print("\n[1] Environment Configuration:")
    print(tabulate(env_rows, headers=["Variable", "Status / Value"], tablefmt="grid"))

    # 2. Test Normalization Engine with English Date Cases
    print_banner("🧪 [2] Data Normalization (English-Only Date Tests)")
    sample_dates = [
        "Jan",
        "Feb 2026",
        "March 3rd 2026",
        "03/03/26",
        "15 Mar 2026",
        "10-Oct-2025",
        "Dec 2025",
        "2026-11-20",
        "invalid_date_xyz"
    ]
    date_results = []
    for d in sample_dates:
        iso, month = parse_full_date(d)
        m_only = parse_month_only_field(d)
        date_results.append([d, str(iso), str(month), str(m_only)])
    print(tabulate(date_results, headers=["Input String", "Full ISO Date", "Month (YYYY-MM)", "Month-Only Parser"], tablefmt="grid"))

    # 3. Test Live monday.com Connection if key provided
    print_banner("🌐 [3] Live monday.com Connection Check")
    if not monday_api_key or monday_api_key.startswith("your_"):
        print("⚠️ MONDAY_API_KEY is not configured. Skipping live API query.")
        return

    client = MondayClient(api_key=monday_api_key)
    try:
        conn = client.test_connection()
        print("✅ Connection successful!")
        print(f"   Authenticated User: {conn.get('user', {}).get('name')} ({conn.get('user', {}).get('email')})")
        print(f"   Account Name:       {conn.get('account', {}).get('name')} (ID: {conn.get('account', {}).get('id')})")
    except MondayAPIError as exc:
        print(f"❌ Connection failed: {exc}")
        return

    # Check Deals Board
    if deals_board_id and not deals_board_id.startswith("your_"):
        print_banner(f"📊 Deals Board Schema (ID: {deals_board_id})")
        try:
            meta, items = client.fetch_board_items(deals_board_id)
            print(f"Board Name: {meta.get('name')}")
            print(f"Total Items Retrieved: {len(items)}")
            cols = [[c.get("id"), c.get("title"), c.get("type")] for c in meta.get("columns", [])]
            print(tabulate(cols, headers=["Column ID", "Column Title", "Type"], tablefmt="grid"))
        except Exception as e:
            print(f"❌ Error querying Deals board: {e}")

    # Check Work Orders Board
    if wo_board_id and not wo_board_id.startswith("your_"):
        print_banner(f"🛠️ Work Orders Board Schema (ID: {wo_board_id})")
        try:
            meta, items = client.fetch_board_items(wo_board_id)
            print(f"Board Name: {meta.get('name')}")
            print(f"Total Items Retrieved: {len(items)}")
            cols = [[c.get("id"), c.get("title"), c.get("type")] for c in meta.get("columns", [])]
            print(tabulate(cols, headers=["Column ID", "Column Title", "Type"], tablefmt="grid"))
        except Exception as e:
            print(f"❌ Error querying Work Orders board: {e}")

    # 4. Check Groq API Connectivity
    print_banner("⚡ [4] Groq LLM API Connection Check")
    from groq import Groq
    keys_to_test = [
        ("GROQ_API_KEY (Primary)", groq_key),
        ("GROQ_API_KEY2 (Fallback)", groq_key2)
    ]
    tested_any = False
    for label, k in keys_to_test:
        if k and not k.startswith("your_"):
            tested_any = True
            try:
                groq_client = Groq(api_key=k)
                test_resp = groq_client.chat.completions.create(
                    model=groq_model,
                    messages=[{"role": "user", "content": "Respond with 'OK' if you can read this."}],
                    max_tokens=10
                )
                reply = test_resp.choices[0].message.content.strip()
                print(f"✅ {label}: Connected! Model: {groq_model} (Response: '{reply}')")
            except Exception as e:
                print(f"❌ {label}: Failed -> {e}")
        else:
            print(f"ℹ️ {label}: Not configured.")

    if not tested_any:
        print("⚠️ No valid Groq API keys configured in .env.")

    print_banner("✅ Diagnostics Completed")


if __name__ == "__main__":
    run_diagnostics()
