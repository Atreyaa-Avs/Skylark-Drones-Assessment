# Build Prompt: Founder BI Agent for monday.com

Copy everything below into your IDE's AI assistant (Cursor / Claude Code / Windsurf) as the initial project prompt.

---

## Prompt

Build a conversational AI agent that answers founder-level business intelligence questions by querying two monday.com boards: **Work Orders** (project execution data) and **Deals** (sales pipeline data). The underlying data is real-world messy (inconsistent date formats, mixed-language month abbreviations, missing fields, inconsistent naming).

### Tech stack
- Python 3.11+
- Chainlit for the conversational UI (`pip install chainlit`)
- Anthropic Claude API (or OpenAI, your choice) as the reasoning/agent LLM, with tool-calling
- monday.com GraphQL API (`https://api.monday.com/v2`) via direct HTTP calls, OR the monday.com MCP server if available — do NOT hardcode or cache CSV data; every query must hit the live API
- python-dotenv for config (MONDAY_API_KEY, ANTHROPIC_API_KEY, board IDs)

### Architecture
1. **monday.com client module** (`monday_client.py`)
   - Authenticate with API token from env var
   - Function to fetch all items from a board by board ID, paginated (monday.com caps at 500 items/page — handle cursors)
   - Function to fetch board schema/column metadata so the agent knows column types and names dynamically (don't hardcode column names — read them from the API)
   - Basic retry/backoff on API errors and rate limits (monday.com returns 429s)

2. **Data normalization layer** (`normalize.py`)
   - Parse inconsistent date formats (e.g., "Jan", "fev", "March 3rd 2026", "03/03/26") into a canonical date/month representation. Handle multi-language month abbreviations (English + at least Spanish/Portuguese given "fev" = February in PT).
   - Normalize text fields: trim whitespace, standardize casing, collapse near-duplicate category names (e.g., "Energy", "energy sector", "ENERGY ") into one canonical value.
   - Flag and log rows with missing critical fields (amount, status, sector, dates) rather than silently dropping them.
   - Return both the cleaned dataset AND a data-quality report (counts of nulls, malformed dates, etc. per column) that the agent can reference when answering.

3. **Query/agent layer** (`agent.py`)
   - Use Claude with tool-calling. Define tools like `fetch_work_orders()`, `fetch_deals()`, `get_data_quality_report()` that the model can call.
   - System prompt should instruct the model to: interpret founder-level natural language questions, decide which board(s) to query, ask a clarifying question ONLY when the query is genuinely ambiguous (e.g., "this quarter" with no year specified, "pipeline" without knowing if they mean count or value), otherwise make a reasonable assumption and state it.
   - Always surface data quality caveats relevant to the answer (e.g., "3 of 40 deals in the energy sector have no close date, so this may undercount").
   - Support cross-board queries (e.g., joining deals won against work orders to check delivery performance).

4. **Chainlit UI** (`app.py`)
   - Simple chat interface using `@cl.on_message`
   - Maintain conversation history in session for follow-up questions
   - On startup, do a lightweight connection check to monday.com and show which boards/columns were detected

5. **"Leadership update" feature** (optional but implement it)
   - A command or intent (e.g., user says "prepare a leadership update" or "/summary") that generates a structured executive summary: pipeline health, sector breakdown, notable risks/data gaps, formatted in clean markdown suitable for pasting into a doc or email.

### Data resilience requirements (critical — this is explicitly graded)
- Never crash or return empty on missing/null values — degrade gracefully and explain what's missing
- Handle multiple date formats and mixed-language month names
- Normalize inconsistent naming/casing across sector, status, and category fields
- Always disclose data quality issues relevant to the specific answer, not as a generic disclaimer dump

### Deliverables to produce alongside the code
- `README.md`: architecture overview, setup instructions (env vars needed, how to get a monday.com API token, how to set board IDs, how to run locally with `chainlit run app.py -w`)
- `DECISION_LOG.md` (max 2 pages): key assumptions, trade-offs, what you'd do with more time, and how you interpreted "leadership updates"
- `requirements.txt`
- Deployment config for Render, Railway, or Hugging Face Spaces (whichever is simplest) so the app is reachable via a public URL

### Constraints
- Do not hardcode any CSV/sample data into the code — everything must be fetched live from monday.com at query time
- Read-only access only — no writes back to monday.com
- Keep the agent's tool-calling loop bounded (max iterations) to avoid runaway API calls

Start by scaffolding the project structure and the monday.com client with a test script that lists both boards' columns, so we can confirm the connection works before building the agent logic.

---

## Before you paste this in
Fill in / have ready:
- Your monday.com API token (Profile picture → Developers → My Access Tokens)
- The board IDs for your Work Orders and Deals boards (visible in each board's URL)
- Which LLM API key you're using (Anthropic or OpenAI)
