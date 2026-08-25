"""
agent.py - Founder Business Intelligence Agent with Tool-Calling & Deep Data-Aware Reasoning

Features:
- Powered by Groq LLM (e.g. Llama 3.3 70B Versatile / GPT-OSS) for ultra-fast, high-precision BI analysis.
- Live dynamic integration with monday.com boards (Deals & Work Orders).
- Full normalization & data resilience with real column mapping and data-quality caveats.
- Specialized calculation tools:
    * fetch_deals: pipeline value, win rate (Won/(Won+Dead)), stage funnels, sector breakdown
    * fetch_work_orders: billing/collection metrics, invoice status, overdue risk analysis
    * get_data_quality_report: per-column nulls, % null, normalization actions
    * analyze_cross_board_performance: cross-board join (Deal Name <-> Deal name masked)
- Executive Leadership Update generation with 5-pillar structure.
"""

import os
import json
import time
import logging
from datetime import datetime, date
from difflib import SequenceMatcher
from typing import Dict, Any, List, Optional, Tuple
from dotenv import load_dotenv

from monday_client import MondayClient, MondayAPIError, get_monday_client
from normalize import (
    normalize_deals_data,
    normalize_work_orders_data,
    data_quality_report,
    DataQualityReport,
    DEAL_STAGE_ORDER
)

load_dotenv()
logger = logging.getLogger("agent")

SYSTEM_PROMPT = """You are an elite Founder Business Intelligence (BI) Agent for an enterprise organization.
Your primary role is to answer strategic, operational, and financial questions by querying live data from monday.com:
1. 'Deals' board (Deal_funnel_Data): sales pipeline, deal values, stages, win rates, close dates, sector breakdown.
2. 'Work Orders' board (Work_Order_Tracker_Data): project execution, billing/collected amounts, invoice statuses, operational milestones, fulfillment.

### Real Column Names & Data Context:
- **Deals board columns**: `Deal Name`, `Owner code`, `Client Code`, `Deal Status` (Won, Dead, Open, On Hold), `Close Date (A)`, `Closure Probability`, `Masked Deal value`, `Tentative Close Date`, `Deal Stage`, `Product deal`, `Sector/service`, `Created Date`.
- **Work Orders board columns**: `Deal name masked`, `Customer Name Code`, `Serial #`, `Nature of Work`, `Execution Status`, `Data Delivery Date`, `Probable Start Date`, `Probable End Date`, `BD/KAM Personnel code`, `Sector`, `Amount in Rupees (Excl of GST) (Masked)`, `Billed Value in Rupees (Excl/Incl of GST.) (Masked)`, `Collected Amount in Rupees (Incl of GST.) (Masked)`, `Amount Receivable (Masked)`, `Invoice Status`, `WO Status (billed)`, `Billing Status`, `Collection status`, `Actual Billing Month`, etc.

### Core Operating Principles:
1. **Founder-Level Answers**: Be direct, concise, and executive-ready. Lead with the headline metric/number first, followed by clear structured breakdowns, totals, conversion rates, and actionable insights.
2. **Accurate BI Formulas**:
   - **Win Rate**: `Won / (Won + Dead)` based on `Deal Status` (exclude Open/On Hold from denominator).
   - **Active Pipeline**: Sum of `Masked Deal value` for deals where `Deal Status == 'Open'`.
   - **Work Orders at Risk**:
     * Stuck billing (`Billing Status == 'Stuck'` or `Invoice Status == 'Stuck'`)
     * Blank/Unknown WO status (`WO Status (billed) == 'Unknown'`)
     * Overdue projects (`Probable End Date` is in the past and project is not completed/closed).
   - **Cross-Board Joins**: Match `Deal Name` (Deals) against `Deal name masked` (Work Orders) to evaluate won-to-delivery conversion.
3. **Query Understanding & Mapping**:
   - "pipeline" → open deals by `Deal Stage` / `Deal Status`
   - "revenue" → clarify if ambiguous (could mean closed deal value, invoiced amount, or collected amount); if the context implies sales pipeline use deal value, if fulfillment use billed/collected.
   - "sector" → `Sector/service` or `Sector`
   - Time ranges like "this quarter" without a year default to the current calendar quarter with the assumption stated.
4. **Contextual Data Quality Caveats**:
   - Whenever you calculate metrics touching a field with >10% nulls or known unreliability, ALWAYS mention that caveat inline in the response (not just as a generic disclaimer).
   - `Collection status` is **100% empty / untracked** across all work orders. If asked about collection status, explicitly state it is not currently tracked by operations rather than returning 0.
   - `Closure Probability` is blank for ~75% of rows (only tracked for open deals).
   - `WO Status (billed)` is blank for ~42% of rows (treated as Unknown).
   - `Close Date (A)` is expected to be blank for open deals.
5. **No Hallucinations**: Calculate metrics strictly from the tool responses.
"""

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "fetch_deals",
            "description": "Fetches and analyzes live sales deals from the Deals board (Deal_funnel_Data), returning pipeline values, win rate, stage funnel distribution, sector breakdown, and data caveats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sector_filter": {
                        "type": ["string", "null"],
                        "description": "Optional filter by canonical sector (e.g. 'Mining', 'Renewables', 'Railways', 'Powerline', 'Construction', 'Others')."
                    },
                    "status_filter": {
                        "type": ["string", "null"],
                        "description": "Optional filter by Deal Status ('Won', 'Dead', 'Open', 'On Hold')."
                    },
                    "stage_filter": {
                        "type": ["string", "null"],
                        "description": "Optional filter by Deal Stage."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_work_orders",
            "description": "Fetches and analyzes live project execution records from the Work Orders board (Work_Order_Tracker_Data), including billing/invoicing status, collected amounts, at-risk work orders (stuck billing, overdue end dates), and operational data caveats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sector_filter": {
                        "type": ["string", "null"],
                        "description": "Optional filter by Sector (e.g. 'Mining', 'Renewables', 'Railways', 'Powerline', 'Construction', 'Others')."
                    },
                    "billing_status_filter": {
                        "type": ["string", "null"],
                        "description": "Optional filter by Billing Status ('Billed', 'Not Billed', 'Partially Billed', 'Stuck')."
                    },
                    "at_risk_only": {
                        "type": ["boolean", "null"],
                        "description": "If true, filters specifically for work orders at risk (stuck billing, unknown status, or overdue end date)."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_data_quality_report",
            "description": "Retrieves comprehensive per-column data quality statistics across both Deals and Work Orders boards (null counts, null percentages, normalizations applied, and known untracked fields like Collection status).",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_cross_board_performance",
            "description": "Correlates Won Deals from the Deals board with operational items in the Work Orders board by matching Deal Name to Deal name masked. Analyzes won deals with vs without work orders, fulfillment status, and billing gaps.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }
]


class FounderBIAgent:
    """Conversational BI Agent powered by Groq with live monday.com integration."""

    def __init__(self):
        self.monday_client = get_monday_client()
        self.work_orders_board_id = os.getenv("WORK_ORDERS_BOARD_ID", "")
        self.deals_board_id = os.getenv("DEALS_BOARD_ID", "")
        self.max_tool_iterations = 6

        # In-memory board cache (TTL 120s)
        self._board_cache: Dict[str, Any] = {}
        self._cache_ttl = 120

        self.groq_key = os.getenv("GROQ_API_KEY", "")
        self.groq_key2 = os.getenv("GROQ_API_KEY2", "")

        # Ordered list of candidate Groq API keys for failover / fallback
        raw_keys = [self.groq_key, self.groq_key2]
        self.groq_keys: List[str] = [
            k.strip() for k in raw_keys
            if k and not k.strip().startswith("your_")
        ]
        self.current_key_index: int = 0
        self.model_name = os.getenv("GROQ_MODEL", os.getenv("LLM_MODEL", "llama-3.3-70b-versatile"))

        if self.groq_keys:
            self.provider = "groq"
        else:
            self.provider = "unconfigured"

    def _is_board_configured(self, board_id: str) -> bool:
        return bool(board_id) and not board_id.startswith("your_")

    def _fetch_board_cached(self, board_id: str):
        now = time.time()
        cached = self._board_cache.get(board_id)
        if cached and (now - cached["ts"]) < self._cache_ttl:
            logger.info(f"Cache hit for board {board_id} (age: {now - cached['ts']:.1f}s)")
            return cached["meta"], cached["items"]
        meta, items = self.monday_client.fetch_board_items(board_id)
        self._board_cache[board_id] = {"meta": meta, "items": items, "ts": now}
        logger.info(f"Cache populated for board {board_id} ({len(items)} items)")
        return meta, items

    @staticmethod
    def _fuzzy_name_match(a: str, b: str, threshold: float = 0.70) -> bool:
        if not a or not b:
            return False
        sa = a.lower().strip()
        sb = b.lower().strip()
        if sa == sb or sa in sb or sb in sa:
            return True
        return SequenceMatcher(None, sa, sb).ratio() >= threshold

    def execute_tool(self, tool_name: str, tool_args: Dict[str, Any]) -> Dict[str, Any]:
        """Executes tool calls with deterministic BI calculations and normalization."""
        logger.info(f"Executing tool: {tool_name} with args: {tool_args}")

        try:
            if tool_name == "fetch_deals":
                if not self._is_board_configured(self.deals_board_id):
                    return {"error": "DEALS_BOARD_ID is not configured in .env"}
                meta, raw_items = self._fetch_board_cached(self.deals_board_id)
                items, report = normalize_deals_data(meta, raw_items)

                sector_filter = tool_args.get("sector_filter")
                if sector_filter:
                    items = [i for i in items if i["sector"].lower() == sector_filter.lower()]

                status_filter = tool_args.get("status_filter")
                if status_filter:
                    items = [i for i in items if i.get("deal_status") and i["deal_status"].lower() == status_filter.lower()]

                stage_filter = tool_args.get("stage_filter")
                if stage_filter:
                    items = [i for i in items if i.get("deal_stage") and stage_filter.lower() in i["deal_stage"].lower()]

                # Win Rate calculation: Won / (Won + Dead)
                won_count = sum(1 for i in items if i.get("deal_status") == "Won")
                dead_count = sum(1 for i in items if i.get("deal_status") == "Dead")
                open_count = sum(1 for i in items if i.get("deal_status") == "Open")
                on_hold_count = sum(1 for i in items if i.get("deal_status") == "On Hold")

                closed_decided = won_count + dead_count
                win_rate_pct = round((won_count / closed_decided * 100), 1) if closed_decided > 0 else 0.0

                # Pipeline value (Active / Open deals)
                open_pipeline_val = sum(i["deal_value"] for i in items if i.get("deal_status") == "Open" and i.get("deal_value") is not None)
                won_deal_val = sum(i["deal_value"] for i in items if i.get("deal_status") == "Won" and i.get("deal_value") is not None)
                total_val = sum(i["deal_value"] for i in items if i.get("deal_value") is not None)

                # Sector & Stage breakdowns
                by_status = {}
                by_sector = {}
                by_stage = {}
                for item in items:
                    st = item.get("deal_status") or "Unknown"
                    sec = item.get("sector") or "Unspecified"
                    stg = item.get("deal_stage") or "Unspecified"
                    by_status[st] = by_status.get(st, 0) + 1
                    by_sector[sec] = by_sector.get(sec, 0) + 1
                    by_stage[stg] = by_stage.get(stg, 0) + 1

                # Sample records (compact 5 items)
                clean_sample = [
                    {
                        "deal_name": i.get("deal_name"),
                        "deal_status": i.get("deal_status"),
                        "deal_value": i.get("deal_value"),
                        "deal_stage": i.get("deal_stage"),
                        "sector": i.get("sector")
                    }
                    for i in items[:5]
                ]

                return {
                    "board_name": meta.get("name"),
                    "total_deals_count": len(items),
                    "win_rate_analysis": {
                        "won_deals": won_count,
                        "dead_deals": dead_count,
                        "open_deals": open_count,
                        "on_hold_deals": on_hold_count,
                        "win_rate_pct": win_rate_pct,
                        "formula": "Won / (Won + Dead)"
                    },
                    "financial_metrics": {
                        "active_open_pipeline_value": open_pipeline_val,
                        "total_won_value": won_deal_val,
                        "total_all_deals_value": total_val
                    },
                    "sector_breakdown": by_sector,
                    "stage_breakdown": by_stage,
                    "sample_records": clean_sample,
                    "data_caveats": report.board_caveats[:4]
                }

            elif tool_name == "fetch_work_orders":
                if not self._is_board_configured(self.work_orders_board_id):
                    return {"error": "WORK_ORDERS_BOARD_ID is not configured in .env"}
                meta, raw_items = self._fetch_board_cached(self.work_orders_board_id)
                items, report = normalize_work_orders_data(meta, raw_items)

                sector_filter = tool_args.get("sector_filter")
                if sector_filter:
                    items = [i for i in items if i["sector"].lower() == sector_filter.lower()]

                billing_status_filter = tool_args.get("billing_status_filter")
                if billing_status_filter:
                    items = [i for i in items if i.get("billing_status") and i["billing_status"].lower() == billing_status_filter.lower()]

                today_str = date.today().isoformat()

                # Identify at-risk work orders
                at_risk_items = []
                for wo in items:
                    is_at_risk = False
                    risk_reasons = []

                    if wo.get("billing_status") == "Stuck" or wo.get("invoice_status") == "Stuck":
                        is_at_risk = True
                        risk_reasons.append("Stuck Billing/Invoicing")

                    if wo.get("wo_status_billed") == "Unknown":
                        is_at_risk = True
                        risk_reasons.append("Blank WO Status (Unknown)")

                    end_d = wo.get("probable_end_date")
                    if end_d and end_d < today_str and wo.get("wo_status_billed") != "Closed":
                        is_at_risk = True
                        risk_reasons.append(f"Overdue End Date ({end_d})")

                    if is_at_risk:
                        at_risk_items.append({
                            "deal_name": wo.get("deal_name_masked"),
                            "customer": wo.get("customer_name_code"),
                            "sector": wo.get("sector"),
                            "wo_status": wo.get("wo_status_billed"),
                            "invoice_status": wo.get("invoice_status"),
                            "billing_status": wo.get("billing_status"),
                            "amount_excl_gst": wo.get("amount_excl_gst"),
                            "billed_excl_gst": wo.get("billed_excl_gst"),
                            "probable_end_date": wo.get("probable_end_date"),
                            "risk_reasons": risk_reasons
                        })

                if tool_args.get("at_risk_only"):
                    items = [i for i in items if any(r["deal_name"] == i.get("deal_name_masked") for r in at_risk_items)]

                # Financial aggregates
                total_amount_excl = sum(i["amount_excl_gst"] for i in items if i.get("amount_excl_gst") is not None)
                total_billed_excl = sum(i["billed_excl_gst"] for i in items if i.get("billed_excl_gst") is not None)
                total_collected_incl = sum(i["collected_incl_gst"] for i in items if i.get("collected_incl_gst") is not None)
                total_receivable = sum(i["amount_receivable"] for i in items if i.get("amount_receivable") is not None)

                by_invoice_status = {}
                by_billing_status = {}
                by_wo_status = {}
                by_sector = {}

                for item in items:
                    inv = item.get("invoice_status") or "None"
                    bill = item.get("billing_status") or "None"
                    wo_st = item.get("wo_status_billed") or "Unknown"
                    sec = item.get("sector") or "Unspecified"

                    by_invoice_status[inv] = by_invoice_status.get(inv, 0) + 1
                    by_billing_status[bill] = by_billing_status.get(bill, 0) + 1
                    by_wo_status[wo_st] = by_wo_status.get(wo_st, 0) + 1
                    by_sector[sec] = by_sector.get(sec, 0) + 1

                return {
                    "board_name": meta.get("name"),
                    "total_work_orders_count": len(items),
                    "financial_totals": {
                        "contract_value_excl_gst": total_amount_excl,
                        "billed_value_excl_gst": total_billed_excl,
                        "collected_incl_gst": total_collected_incl,
                        "amount_receivable": total_receivable,
                        "billing_gap_excl_gst": total_amount_excl - total_billed_excl
                    },
                    "status_counts": {
                        "wo_status_billed": by_wo_status,
                        "invoice_status": by_invoice_status,
                        "billing_status": by_billing_status
                    },
                    "sector_breakdown": by_sector,
                    "at_risk_summary": {
                        "total_at_risk_count": len(at_risk_items),
                        "at_risk_sample": at_risk_items[:5]
                    },
                    "data_caveats": report.board_caveats[:4],
                    "collection_status_note": "Collection status is 100% empty across all rows (untracked in ops)."
                }

            elif tool_name == "get_data_quality_report":
                d_meta, d_raw = self._fetch_board_cached(self.deals_board_id)
                w_meta, w_raw = self._fetch_board_cached(self.work_orders_board_id)
                return data_quality_report(d_meta, d_raw, w_meta, w_raw)

            elif tool_name == "analyze_cross_board_performance":
                d_meta, d_raw = self._fetch_board_cached(self.deals_board_id)
                d_items, d_rep = normalize_deals_data(d_meta, d_raw)

                w_meta, w_raw = self._fetch_board_cached(self.work_orders_board_id)
                w_items, w_rep = normalize_work_orders_data(w_meta, w_raw)

                won_deals = [d for d in d_items if d.get("deal_status") == "Won"]

                matched_won_deals = []
                unmatched_won_deals = []

                for deal in won_deals:
                    d_name = deal.get("deal_name") or ""
                    c_code = deal.get("client_code") or ""

                    matching_wos = [
                        wo for wo in w_items
                        if self._fuzzy_name_match(d_name, wo.get("deal_name_masked", "")) or
                           (c_code and self._fuzzy_name_match(c_code, wo.get("customer_name_code", "")))
                    ]

                    if matching_wos:
                        matched_won_deals.append({
                            "deal_name": d_name,
                            "deal_value": deal.get("deal_value"),
                            "sector": deal.get("sector"),
                            "wo_count": len(matching_wos),
                            "wo_statuses": [wo.get("wo_status_billed") for wo in matching_wos]
                        })
                    else:
                        unmatched_won_deals.append({
                            "deal_name": d_name,
                            "deal_value": deal.get("deal_value"),
                            "sector": deal.get("sector")
                        })

                return {
                    "total_won_deals": len(won_deals),
                    "won_deals_with_work_orders": len(matched_won_deals),
                    "won_deals_without_work_orders": len(unmatched_won_deals),
                    "conversion_rate_pct": round(len(matched_won_deals) / len(won_deals) * 100, 1) if won_deals else 0,
                    "unmatched_sample": unmatched_won_deals[:5],
                    "matched_sample": matched_won_deals[:5]
                }

            else:
                return {"error": f"Unknown tool name: {tool_name}"}

        except MondayAPIError as exc:
            return {"error": f"monday.com API Error: {str(exc)}"}
        except Exception as exc:
            logger.exception("Error executing tool")
            return {"error": f"Tool execution failed: {str(exc)}"}

    def _call_chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        temperature: float = 0.1,
        progress_callback=None
    ):
        """Calls Groq chat completion API with automatic failover to secondary keys on rate limits or API errors."""
        from groq import Groq

        if not self.groq_keys:
            raise RuntimeError("No valid Groq API keys configured in .env.")

        start_index = self.current_key_index
        total_keys = len(self.groq_keys)

        for attempt in range(total_keys):
            idx = (start_index + attempt) % total_keys
            active_key = self.groq_keys[idx]
            masked_key = f"{active_key[:8]}...{active_key[-4:]}" if len(active_key) > 12 else "***"

            try:
                client = Groq(api_key=active_key)
                kwargs: Dict[str, Any] = {
                    "model": self.model_name,
                    "messages": messages,
                    "temperature": temperature,
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = tool_choice

                response = client.chat.completions.create(**kwargs)
                # Successful call: update current active key index
                self.current_key_index = idx
                return response

            except Exception as exc:
                err_str = str(exc)
                logger.warning(
                    f"Groq API call failed with key #{idx + 1} ({masked_key}): {err_str}"
                )

                if attempt < total_keys - 1:
                    next_idx = (start_index + attempt + 1) % total_keys
                    fallback_label = f"GROQ_API_KEY{next_idx + 1 if next_idx > 0 else ''}"
                    msg_notice = (
                        f"⚠️ Groq API key #{idx + 1} encountered an issue ({err_str[:60]}...). "
                        f"Falling back to `{fallback_label}`..."
                    )
                    logger.info(msg_notice)
                    if progress_callback:
                        progress_callback(msg_notice)
                    continue
                else:
                    logger.error(f"All configured Groq API keys ({total_keys}) failed.")
                    raise exc

    def run_turn(self, conversation_history: List[Dict[str, Any]], progress_callback=None) -> str:
        """Runs a multi-turn conversation with Groq tool calling and key failover."""
        if self.provider == "unconfigured":
            return (
                "⚠️ **Groq API Key Missing**: Please configure `GROQ_API_KEY` (or `GROQ_API_KEY2`) "
                "in your `.env` file to enable the BI reasoning agent."
            )

        groq_msgs: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for m in conversation_history:
            role = m.get("role")
            if role in ("user", "assistant", "system"):
                groq_msgs.append({"role": role, "content": m.get("content", "")})

        iteration = 0
        while iteration < self.max_tool_iterations:
            iteration += 1
            try:
                response = self._call_chat_completion(
                    messages=groq_msgs,
                    tools=TOOLS_SCHEMA,
                    tool_choice="auto",
                    temperature=0.1,
                    progress_callback=progress_callback
                )
            except Exception as exc:
                return f"⚠️ **Groq API Error**: {str(exc)}"

            choice = response.choices[0]
            msg = choice.message

            if msg.tool_calls:
                groq_msgs.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        }
                        for tc in msg.tool_calls
                    ]
                })

                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except Exception:
                        fn_args = {}

                    if progress_callback:
                        progress_callback(f"Querying live monday.com ({fn_name})...")

                    tool_out = self.execute_tool(fn_name, fn_args)
                    groq_msgs.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(tool_out)
                    })
            else:
                return msg.content or ""

        return "⚠️ Tool iteration limit reached. Please refine your query."

    def generate_leadership_update(self, progress_callback=None) -> str:
        """Generates a structured executive leadership update in Markdown."""
        prompt = (
            "Generate a comprehensive, structured Markdown Executive Leadership Update for the founders. "
            "Execute the tools to fetch live data from both the Deals and Work Orders boards, as well as cross-board joins. "
            "Cover the following 5 structured sections:\n\n"
            "1. **Executive Headline & Top-Line Metrics**: Total active pipeline value, total won deals value, total billed/collected value, and overall Win Rate (Won / (Won + Dead)).\n"
            "2. **Sales Pipeline & Funnel Health**: Pipeline value and deal count broken down by Deal Stage (ordered funnel) and Sector (Renewables, Mining, Railways, Powerline, Construction, Others).\n"
            "3. **Operational Delivery & Billing Performance**: Work orders delivery status, total billed vs to-be-billed gap, and Invoice Status breakdown.\n"
            "4. **Work Orders at Risk**: Highlight specific projects at risk due to stuck billing/invoicing, blank/unknown WO status (~42%), or overdue probable end dates.\n"
            "5. **Data Quality & Visibility Gaps**: Cite critical data caveats that leadership must know (e.g. Collection status is 100% untracked by operations, Closure Probability is blank for ~75% of deals, WO status blank for ~42%)."
        )
        return self.run_turn([{"role": "user", "content": prompt}], progress_callback=progress_callback)
