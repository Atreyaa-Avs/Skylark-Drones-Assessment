# Decision Log & Architecture Notes

This document outlines key technical decisions, data-quality resolutions, architectural trade-offs, and design interpretations for the **Founder BI Agent for monday.com**.

---

## 1. Data-Aware Normalization & Board Quality Fixes

### Board 1: Deals (`Deal_funnel_Data`)
- **Stray Header Rows**: Export artifacts duplicated column headers (`Deal Status`) inside data rows. Implemented automated row-level rejection in `normalize_deals_data()` for values matching `"deal status"`.
- **Closure Probability**: Blank in ~75% of rows (258/346). Verified as expected domain behavior (populated only for active open pipeline deals).
- **Close Date (A)**: Frequently blank for open/in-progress deals. Treated as expected non-error state; tracked without triggering data corruption warnings.
- **Ordered Funnel Stages**: Preserved sequence from `A. Lead Generated` through `O. Not Relevant at all` and `Project Completed` rather than treating stages as flat categories.
- **Sector Alignment**: Canonicalized `Sector/service` variants to align with Work Orders `Sector` (`Renewables`, `Mining`, `Railways`, `Powerline`, `Construction`, `Others`).

### Board 2: Work Orders (`Work_Order_Tracker_Data`)
- **Collection Status (100% Untracked)**: `Collection status` is completely empty across all 176 records. Marked as structurally untracked in `DataQualityReport` and flagged inline whenever queried to avoid misleading zero values.
- **Billing Status Inconsistencies**: Near-duplicate casing variants (e.g. `"BIlled"` vs `"Billed"`, `"not billed yet"`) are normalized via `BILLING_STATUS_NORM`.
- **Visit-Numbered Invoice Statuses**: Values like `"Billed- Visit 7"` and `"Billed- Visit 3"` are bucketed into `"Partially Billed"`, capturing multi-tranche billing while keeping high-level operational counts clean.
- **WO Status (billed) Missingness**: Blank for ~42% of rows (74/176). Standardized to `"Unknown"` and never coerced to `"Open"` or `"Closed"`.
- **Two Date Families**: Full dates (`Data Delivery Date`, `Date of PO/LOI`, `Probable Start/End Date`, etc.) and month-only fields (`Expected Billing Month`, `Actual Billing Month`, `Last executed month`) are processed through dedicated parsing pipelines.
- **Monetary (Masked) Rupee Fields**: Treated as genuine numeric floats across calculations without extraneous privacy disclaimers.

### English-Only Date Simplification
- Replaced multi-language month parsing with an English-only month parser (`Jan`..`Dec`, `January`..`December`). Removed non-English token tables and multi-lingual regex pipelines.

---

## 2. Architectural Trade-offs

| Decision | Alternative Considered | Rationale & Trade-off |
| :--- | :--- | :--- |
| **Direct GraphQL HTTP Client with httpx** | monday.com Python SDK / CSV dumps | Zero-dependency overhead, live synchronization, dynamic schema introspection, and exact cursor-based pagination (500 items/page limit) without stale CSV dumps. |
| **Deterministic Data Normalization Layer** | Pure LLM normalization | Mathematical precision, zero hallucination in financial sums/counts, and ~80% token savings compared to passing messy raw JSON to LLMs. |
| **Groq LPU Inference (Llama 3.3 70B / GPT-OSS)** | Cloud proprietary APIs (OpenAI / Anthropic) | Sub-second tool calling and conversational responses with near-zero latency for live interactive founder Q&A. |
| **Per-Column Data Quality Tracking** | Monolithic quality report | Granular per-column null % tracking allows the agent to generate targeted, query-specific inline caveats (e.g. only mentioning date nulls when date-based forecasting is queried). |
| **Fuzzy Cross-Board Matching (0.70 threshold)** | Exact string match | Accounts for minor naming variances between `Deal Name` on Deals board and `Deal name masked` / `Customer Name Code` on Work Orders board. |

---

## 3. How the "Leadership Update" Feature Was Interpreted

Founders require an actionable, 360-degree synthesis that connects top-line sales velocity directly to bottom-line project fulfillment. The **Leadership Update** (`/summary` or `/leadership`) generates a 5-pillar executive report:

1. **Executive Headline & Top-Line Financials**:
   - Total Active Pipeline Value (`Masked Deal value` for `Deal Status == 'Open'`).
   - Total Won Deals Value & Win Rate (`Won / (Won + Dead)`).
   - Total Work Orders Billed vs. Collected Value.
2. **Sales Pipeline & Funnel Health**:
   - Deal counts and values grouped by ordered funnel stages (`A. Lead Generated` → `Won` / `Lost`).
   - Sector distribution (`Renewables`, `Mining`, `Railways`, `Powerline`, `Construction`).
3. **Operational Delivery & Billing Performance**:
   - Work order distribution by `WO Status (billed)` and `Invoice Status`.
   - Invoicing gap: Contract Value vs. Billed Value.
4. **Work Orders at Risk**:
   - Projects with stuck billing/invoicing (`Billing Status == 'Stuck'` or `Invoice Status == 'Stuck'`).
   - Projects with unpopulated/unknown status (`WO Status == 'Unknown'`).
   - Projects with overdue `Probable End Date` vs. current date.
5. **Critical Data Quality & Visibility Gaps**:
   - Transparent disclosure of blind spots: `Collection status` 100% unrecorded in ops, ~42% blank WO status, and ~75% unpopulated closure probability.

---

## 4. What We Would Do With More Time

1. **Visual Funnel & Burndown Charts**: Direct integration of Chart.js / Plotly into Chainlit for visual pipeline conversion funnels and billing realization graphs.
2. **Automated Monday.com Webhooks**: Real-time push alerts for high-value won deals without associated work orders or at-risk delivery deadlines.
3. **Historical Cycle-Time Forecasting**: Predictive models to estimate lead-to-won and won-to-delivered cycle times by sector.

---

## 5. Tech Stack

| Layer / Domain | Technology | Purpose & Implementation Details |
| :--- | :--- | :--- |
| **Language & Runtime** | Python 3.11 / 3.10+ | Core application runtime utilizing modern type hints and AsyncIO. |
| **Conversational UI** | [Chainlit](https://chainlit.io/) (`>=1.3.0`) | Interactive web chat interface with live tool-execution step visualization, custom action starters, and streaming support. |
| **LLM & Inference** | [Groq SDK](https://console.groq.com/) (`groq>=0.11.0`) | High-throughput, sub-second LPU inference using `llama-3.3-70b-versatile` with native tool/function calling for executive synthesis. |
| **API Client & Networking** | `httpx` (`>=0.27.0`) | Direct HTTP GraphQL client communicating with monday.com API v2 (`https://api.monday.com/v2`), cursor pagination (`next_items_page`), and exponential backoff retry logic. |
| **Data Validation & Schemas** | `pydantic` (`>=2.8.0`) | Structured data models, quality reporting schemas, and response validation. |
| **Data Normalization & Parsing** | `python-dateutil` (`>=2.9.0`), Python Standard Library (`re`, `difflib`) | Multi-format date parsing, fuzzy string matching (cross-board joins), currency/numeric extraction, and sector/status canonicalization. |
| **Formatting & Diagnostics** | `tabulate` (`>=0.9.0`), `rich` (`>=13.7.0`) | Formatted tabular summaries and terminal diagnostic logs. |
| **Environment Management** | `python-dotenv` (`>=1.0.0`) | Secure local environment variable and secret configuration (`.env`). |
| **Testing & Verification** | `pytest` (`>=8.0.0`) | Automated test suite verifying normalization logic, board quality checks, and client resilience. |
| **Deployment & Containerization** | Docker (`python:3.11-slim`), Render Blueprint (`render.yaml`) | Production containerized deployment and automated cloud hosting. |

---

## 6. Context Window Management & Usage Guidelines

> [!IMPORTANT]
> **Recommended Session Length (Max 2 Messages/Chat)**:
> Please avoid putting more than 2 messages in a single chat session. Because of the limited context window of the model and the large data payloads retrieved during live board tool calls, tap on **"New Chat"** in the top-left corner to start a fresh chat when asking new questions.

