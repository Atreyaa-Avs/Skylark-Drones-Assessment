"""
app.py - Chainlit Conversational Interface for Founder BI Agent

Features:
- Animated loading indicator & progress step during agent startup initialization.
- Startup diagnostic check on monday.com API & Board schemas (executed via async threads).
- Interactive multi-turn conversational interface with session history.
- Live progress indicators for monday.com GraphQL tool calls.
- One-click executive starter action buttons (/summary, pipeline, delayed work orders, data quality).
- One-click / Command executive leadership updates (/summary, /leadership).
- Graceful error messaging and environment setup guidance.
"""

import os
import asyncio
import chainlit as cl
from dotenv import load_dotenv

from monday_client import MondayClient, MondayAPIError, get_monday_client
from agent import FounderBIAgent

load_dotenv()


async def process_bi_query(user_query: str):
    """Core handler to process founder queries and leadership summaries."""
    agent: FounderBIAgent = cl.user_session.get("agent")
    if not agent:
        agent = FounderBIAgent()
        cl.user_session.set("agent", agent)

    history = cl.user_session.get("history", [])

    # Check for leadership update commands
    if user_query.lower() in ("/summary", "/leadership", "prepare a leadership update", "leadership update"):
        async with cl.Step(name="Executive Update Generator", type="run") as step:
            step.output = "Querying live Deals & Work Orders boards for executive synthesis..."
            await step.update()

            def progress_callback(info: str):
                pass

            response_text = await asyncio.to_thread(
                agent.generate_leadership_update, progress_callback
            )
            step.output = "Executive summary generated from live board data."
            await step.update()

        history.append({"role": "user", "content": user_query})
        history.append({"role": "assistant", "content": response_text})
        cl.user_session.set("history", history)

        await cl.Message(content=response_text).send()
        return

    # Standard conversational turn
    history.append({"role": "user", "content": user_query})

    async with cl.Step(name="Founder BI Agent Reasoning", type="run") as step:
        step.output = "Analyzing question and identifying required monday.com boards..."
        await step.update()

        step_logs = []
        def step_callback(msg: str):
            step_logs.append(msg)

        response_text = await asyncio.to_thread(
            agent.run_turn, history, step_callback
        )

        if step_logs:
            step.output = "\n".join([f"• {log}" for log in step_logs])
        else:
            step.output = "Analysis complete."
        await step.update()

    history.append({"role": "assistant", "content": response_text})
    cl.user_session.set("history", history)

    await cl.Message(content=response_text).send()


@cl.on_chat_start
async def on_chat_start():
    """Initializes the chat session with a loading indicator, tests live connections, and displays board schema info."""
    cl.user_session.set("history", [])

    # 1. Immediately send loading indicator to inform the user
    loading_msg = cl.Message(
        content=(
            "⏳ **Initializing Founder BI Agent...**\n"
            "*Please wait while we boot the reasoning engine, verify API credentials, and sync monday.com boards.*"
        )
    )
    await loading_msg.send()

    # 2. Run diagnostic checks with live Step updates
    async with cl.Step(name="🚀 Initializing Founder BI Agent & Syncing Workspace", type="run") as init_step:
        init_step.output = "⚙️ Booting LLM Reasoning Engine..."
        await init_step.update()

        agent = await asyncio.to_thread(FounderBIAgent)
        cl.user_session.set("agent", agent)

        monday_api_key = os.getenv("MONDAY_API_KEY", "")
        wo_board_id = os.getenv("WORK_ORDERS_BOARD_ID", "")
        deals_board_id = os.getenv("DEALS_BOARD_ID", "")
        llm_provider = agent.provider

        status_lines = []
        status_lines.append("### 🚀 Founder BI Agent Initialized")
        status_lines.append(f"**LLM Reasoning Engine:** `{llm_provider.upper()}` ({agent.model_name})")

        if llm_provider == "unconfigured":
            status_lines.append("> ⚠️ **Notice**: `GROQ_API_KEY` is not set in `.env`. Please add your Groq API key to enable AI reasoning.")
        elif len(agent.groq_keys) > 1:
            status_lines.append(f"> 🛡️ **Failover Active**: {len(agent.groq_keys)} Groq API keys configured (Auto-fallback to `GROQ_API_KEY2` if primary key hits rate limits).")

        wo_info_str = "❌ Not connected"
        deals_info_str = "❌ Not connected"

        if not monday_api_key or monday_api_key.startswith("your_"):
            status_lines.append("\n> ⚠️ **Configuration Notice**: `MONDAY_API_KEY` is not configured in `.env`.")
            init_step.output = "⚠️ monday.com API key not configured."
            await init_step.update()
        else:
            try:
                init_step.output = "🔌 Authenticating with monday.com GraphQL API..."
                await init_step.update()

                client = get_monday_client()
                conn_res = await asyncio.to_thread(client.test_connection)
                user_name = conn_res.get("user", {}).get("name", "User")
                account_name = conn_res.get("account", {}).get("name", "Account")
                status_lines.append(f"**monday.com Connection:** ✅ Connected as **{user_name}** ({account_name})")

                init_step.output = "📊 Syncing Deals & Work Orders board schemas..."
                await init_step.update()

                # Check Deals Board
                if deals_board_id and not deals_board_id.startswith("your_"):
                    try:
                        d_meta, d_items = await asyncio.to_thread(client.fetch_board_items, deals_board_id, limit_per_page=500)
                        col_names = [c.get("title") for c in d_meta.get("columns", []) if c.get("title")]
                        deals_info_str = f"✅ **{d_meta.get('name')}** (ID: `{deals_board_id}`) — {len(d_items)} records\n  *Columns detected:* {', '.join(col_names[:7])}{'...' if len(col_names) > 7 else ''}"
                    except Exception as e:
                        deals_info_str = f"⚠️ Error loading Deals board ({deals_board_id}): {str(e)}"
                else:
                    deals_info_str = "⚠️ `DEALS_BOARD_ID` not configured in `.env`"

                # Check Work Orders Board
                if wo_board_id and not wo_board_id.startswith("your_"):
                    try:
                        w_meta, w_items = await asyncio.to_thread(client.fetch_board_items, wo_board_id, limit_per_page=500)
                        col_names = [c.get("title") for c in w_meta.get("columns", []) if c.get("title")]
                        wo_info_str = f"✅ **{w_meta.get('name')}** (ID: `{wo_board_id}`) — {len(w_items)} records\n  *Columns detected:* {', '.join(col_names[:7])}{'...' if len(col_names) > 7 else ''}"
                    except Exception as e:
                        wo_info_str = f"⚠️ Error loading Work Orders board ({wo_board_id}): {str(e)}"
                else:
                    wo_info_str = "⚠️ `WORK_ORDERS_BOARD_ID` not configured in `.env`"

                init_step.output = "✅ monday.com workspace schemas verified successfully."
                await init_step.update()

            except Exception as e:
                status_lines.append(f"\n> ❌ **monday.com API Connection Error**: {str(e)}")
                init_step.output = f"❌ Error: {str(e)}"
                await init_step.update()

    status_lines.append("\n**Detected Boards & Schemas:**")
    status_lines.append(f"- **Deals Board:** {deals_info_str}")
    status_lines.append(f"- **Work Orders Board:** {wo_info_str}")

    status_lines.append("\n---")
    status_lines.append("**💡 Suggested Founder Queries:**")
    status_lines.append("1. *\"What is our total active pipeline value, and how is it distributed across sectors?\"*")
    status_lines.append("2. *\"How many work orders are currently delayed or stuck, and which clients are impacted?\"*")
    status_lines.append("3. *\"Give me a cross-board summary: are won deals being fulfilled on schedule?\"*")
    status_lines.append("4. *\"What data quality gaps or missing dates exist in our pipeline?\"*")
    status_lines.append("5. Type **/summary** or **/leadership** to generate a complete executive update.")

    actions = [
        cl.Action(
            name="quick_query",
            payload={"query": "/summary"},
            label="💼 Leadership Update (/summary)"
        ),
        cl.Action(
            name="quick_query",
            payload={"query": "What is our total active pipeline value and win rate across sectors?"},
            label="💰 Pipeline Health & Win Rates"
        ),
        cl.Action(
            name="quick_query",
            payload={"query": "How many work orders are currently delayed or stuck in billing, and which clients are affected?"},
            label="⚠️ Delayed Work Orders"
        ),
        cl.Action(
            name="quick_query",
            payload={"query": "What data quality gaps, missing dates, and unmapped statuses exist across our boards?"},
            label="🔍 Data Quality Audit"
        ),
    ]

    # Update loading message in-place to the completed initialized state with quick action buttons
    loading_msg.content = "\n".join(status_lines)
    loading_msg.actions = actions
    await loading_msg.update()


@cl.action_callback("quick_query")
async def on_action_quick_query(action: cl.Action):
    """Handles clicks on one-click starter action buttons."""
    query = action.payload.get("query", "")
    if query:
        await cl.Message(content=f"👉 *Action triggered:* **{query}**").send()
        await process_bi_query(query)


@cl.on_message
async def on_message(message: cl.Message):
    """Processes user queries via text input."""
    user_query = message.content.strip()
    await process_bi_query(user_query)
