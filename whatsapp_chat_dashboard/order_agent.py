import json
import os
import re
import sys
from pathlib import Path

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent
from langchain_mcp_adapters.client import MultiServerMCPClient

ROOT = Path(__file__).resolve().parent
MCP_SERVER = ROOT.parent / "mcp_order_server" / "server.py"

ORDER_ID_RE = re.compile(r"\bORD[\s\-]?\d{3,}\b", re.IGNORECASE)


def _extract_order_ids_from_text(text: str) -> list[str]:
    """Pull order IDs from any string."""
    return [re.sub(r"[\s\-]", "", m).upper() for m in ORDER_ID_RE.findall(text)]


async def answer_with_order_tools(messages, memory_context=""):
    """Run the LangGraph agent and return (reply_text, tool_calls_list)."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing from .env")
    client = MultiServerMCPClient({"orders": {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(MCP_SERVER)],
        "env": {**os.environ,
                "ORDER_API_URL": os.getenv("ORDER_API_URL", "http://127.0.0.1:8000").rstrip("/"),
                "ORDER_API_KEY": os.getenv("ORDER_API_KEY", ""),
                "FESTIVE_API_URL": os.getenv("FESTIVE_API_URL", "http://127.0.0.1:8002")},
    }})
    tools = await client.get_tools()
    model = ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        google_api_key=api_key,
        temperature=0,
    )

    # Build the system prompt with memory context
    base_prompt = (
        "You are a WhatsApp customer support assistant for an e-commerce store.\n\n"
        "TOOLS AVAILABLE:\n"
        "- get_order: Look up order status & details. Order IDs are like ORD101. If user says just '101', use 'ORD101'.\n"
        "- get_shipment_details: Get tracking number, carrier, and estimated delivery for an order.\n"
        "- check_holidays: Check if there are active/upcoming festival holidays causing delivery delays.\n"
        "- check_return_eligibility: Check if an item can be returned. Needs order_id AND product_id.\n"
        "- start_return: Create a return request. Needs order_id, product_id, and reason.\n"
        "- get_return_status: Check status of an existing return (needs return_id like RET-1001).\n\n"
        "GENERAL RULES:\n"
        "1. Answer only from tool results. Never invent status, dates, tracking info, or refund amounts.\n"
        "2. If the customer says 'my order' without an ID, check the CUSTOMER MEMORY below.\n"
        "   Look for 'Last known order ID' — if found, use it directly WITHOUT asking again.\n"
        "3. When a customer asks WHY their order is delayed, ALWAYS call check_holidays.\n"
        "4. Keep replies concise, friendly, and helpful.\n"
        "5. If no order ID is known at all (not in memory, not in conversation), then ask for it.\n"
        "6. Use CUSTOMER PREFERENCES to personalize your tone and responses.\n"
        "7. Use PAST CONVERSATION HISTORY to avoid repeating questions.\n\n"
        "RETURN & REFUND PROCEDURES:\n"
        "8. When a customer wants to return an item, ALWAYS call check_return_eligibility FIRST.\n"
        "   You need both the order_id and product_id. If you don't know the product_id,\n"
        "   call get_order first to see the items in the order, then ask which item to return.\n"
        "9. If check_return_eligibility returns eligible=true, tell the customer the refund amount\n"
        "   and ask for confirmation BEFORE calling start_return.\n"
        "10. If check_return_eligibility returns eligible=false, explain the reason politely.\n"
        "    NEVER override the system's decision. NEVER promise a return that was denied.\n"
        "11. After start_return succeeds, give the customer the return ID, expected timeline,\n"
        "    and refund details.\n"
        "12. Never promise compensation or refunds without system confirmation.\n"
        "13. Never claim a refund was processed unless the system confirmed it."
    )

    if memory_context:
        base_prompt += "\n\n" + memory_context

    agent = create_react_agent(model, tools, prompt=base_prompt)
    result = await agent.ainvoke({"messages": messages})

    # ── Extract tool calls from the agent's message trace ──
    tool_calls_log = []
    all_msgs = result.get("messages", [])
    for msg in all_msgs:
        msg_type = type(msg).__name__

        # AIMessage with tool_calls → records what the LLM decided to call
        if msg_type == "AIMessage" and hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls_log.append({
                    "tool_name": tc.get("name", "unknown"),
                    "tool_input": json.dumps(tc.get("args", {}), default=str),
                    "tool_output": "",  # filled in by matching ToolMessage
                    "order_id": "",
                })
                # Try to find an order ID in the args
                args_str = json.dumps(tc.get("args", {}))
                oids = _extract_order_ids_from_text(args_str)
                if oids:
                    tool_calls_log[-1]["order_id"] = oids[0]

        # ToolMessage → the tool's response
        if msg_type == "ToolMessage":
            content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content, default=str)
            tool_name = getattr(msg, "name", "")
            # Match to the last unmatched tool_call with this name
            for tc_entry in reversed(tool_calls_log):
                if tc_entry["tool_name"] == tool_name and not tc_entry["tool_output"]:
                    tc_entry["tool_output"] = content[:2000]
                    # Also try to grab order ID from output
                    if not tc_entry["order_id"]:
                        oids = _extract_order_ids_from_text(content)
                        if oids:
                            tc_entry["order_id"] = oids[0]
                    break

    reply_text = all_msgs[-1].content if all_msgs else ""
    return reply_text, tool_calls_log
