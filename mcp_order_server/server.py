import json
import os
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Order Status")

# ── Shared HTTP helper ──

def _base_url():
    return os.getenv("ORDER_API_URL", "http://127.0.0.1:8000").rstrip("/")

def _api_key():
    return os.getenv("ORDER_API_KEY", "")

def _headers():
    return {"X-API-Key": _api_key()}

def _festive_url():
    return os.getenv("FESTIVE_API_URL", "http://127.0.0.1:8002").rstrip("/")


# =============================================
# EXISTING TOOLS
# =============================================

@mcp.tool()
async def get_order(order_id: str) -> dict:
    """Get current status, shipping info, and status history for one order ID."""
    order_id = order_id.strip().upper()
    if not order_id or len(order_id) > 64:
        return {"error": "Provide a valid order ID."}
    if not _api_key():
        return {"error": "Order API key is not configured."}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{_base_url()}/api/orders/{order_id}", headers=_headers())
        if response.status_code == 404:
            return {"error": f"Order {order_id} was not found."}
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"Order API returned HTTP {exc.response.status_code}. Check API key and API address."}
    except httpx.RequestError as exc:
        return {"error": f"Could not reach the order API ({type(exc).__name__})."}


@mcp.tool()
async def check_holidays() -> dict:
    """Check if there are any active or upcoming festival holidays that may cause delivery delays.
    Call this tool when a customer asks why their order is delayed or about delivery timelines."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            active_resp = await client.get(f"{_festive_url()}/api/holidays/active")
            active_data = active_resp.json() if active_resp.is_success else {"active": []}

            upcoming_resp = await client.get(f"{_festive_url()}/api/holidays/upcoming")
            upcoming_data = upcoming_resp.json() if upcoming_resp.is_success else {"holidays": []}

        result = {
            "today": active_data.get("today", ""),
            "active_holidays": active_data.get("active", []),
            "upcoming_holidays": upcoming_data.get("holidays", []),
        }

        if not result["active_holidays"] and not result["upcoming_holidays"]:
            result["summary"] = "No active or upcoming holidays. Deliveries should proceed normally."
        elif result["active_holidays"]:
            names = ", ".join(h["festival_name"] for h in result["active_holidays"])
            result["summary"] = f"Today is a holiday period: {names}. Deliveries may be delayed."
        else:
            next_h = result["upcoming_holidays"][0]
            result["summary"] = f"Next holiday: {next_h['festival_name']} from {next_h['start_date']} to {next_h['end_date']}."

        return result
    except httpx.RequestError:
        return {"error": "Could not reach the Festive Agent. It may not be running.", "active_holidays": [], "upcoming_holidays": []}


# =============================================
# NEW: Shipment Details Tool
# =============================================

@mcp.tool()
async def get_shipment_details(order_id: str) -> dict:
    """Get shipping/tracking details for an order including carrier, tracking number, and estimated delivery.
    Call this when a customer asks about delivery tracking or shipping status."""
    order_id = order_id.strip().upper()
    if not order_id:
        return {"error": "Provide a valid order ID."}
    if not _api_key():
        return {"error": "Order API key is not configured."}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{_base_url()}/api/orders/{order_id}/shipping", headers=_headers())
        if response.status_code == 404:
            return {"error": f"Order {order_id} was not found."}
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"Order API returned HTTP {exc.response.status_code}."}
    except httpx.RequestError as exc:
        return {"error": f"Could not reach the order API ({type(exc).__name__})."}


# =============================================
# NEW: Return & Refund Tools
# =============================================

@mcp.tool()
async def check_return_eligibility(order_id: str, product_id: str) -> dict:
    """Check if an item from an order is eligible for return.
    ALWAYS call this BEFORE starting a return. It checks:
    - Order must be Delivered
    - Within 7-day return window
    - Item must be returnable
    - Monthly return limit (max 3)
    - Monthly refund limit (max ₹5,000)
    Returns eligible=true/false with the reason."""
    order_id = order_id.strip().upper()
    product_id = product_id.strip().upper()
    if not order_id or not product_id:
        return {"error": "Provide both order_id and product_id."}
    if not _api_key():
        return {"error": "Order API key is not configured."}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{_base_url()}/api/returns/check",
                headers=_headers(),
                json={"order_id": order_id, "product_id": product_id},
            )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", "")
        except Exception:
            detail = f"HTTP {exc.response.status_code}"
        return {"error": f"Eligibility check failed: {detail}"}
    except httpx.RequestError as exc:
        return {"error": f"Could not reach the order API ({type(exc).__name__})."}


@mcp.tool()
async def start_return(order_id: str, product_id: str, reason: str) -> dict:
    """Start a return for a specific item in an order.
    ONLY call this AFTER check_return_eligibility returned eligible=true AND the customer confirmed.
    The system will re-verify eligibility as a safety check.
    Returns the return ID and status."""
    order_id = order_id.strip().upper()
    product_id = product_id.strip().upper()
    reason = reason.strip()
    if not order_id or not product_id or not reason:
        return {"error": "Provide order_id, product_id, and reason."}
    if not _api_key():
        return {"error": "Order API key is not configured."}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{_base_url()}/api/returns/start",
                headers=_headers(),
                json={"order_id": order_id, "product_id": product_id, "reason": reason},
            )
        if response.status_code == 400:
            return response.json()
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", "")
        except Exception:
            detail = f"HTTP {exc.response.status_code}"
        return {"error": f"Return request failed: {detail}"}
    except httpx.RequestError as exc:
        return {"error": f"Could not reach the order API ({type(exc).__name__})."}


@mcp.tool()
async def get_return_status(return_id: str) -> dict:
    """Check the status of an existing return request.
    Call this when a customer asks about their return or refund progress."""
    return_id = return_id.strip().upper()
    if not return_id:
        return {"error": "Provide a valid return ID."}
    if not _api_key():
        return {"error": "Order API key is not configured."}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{_base_url()}/api/returns/{return_id}", headers=_headers())
        if response.status_code == 404:
            return {"error": f"Return {return_id} was not found."}
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"Order API returned HTTP {exc.response.status_code}."}
    except httpx.RequestError as exc:
        return {"error": f"Could not reach the order API ({type(exc).__name__})."}


if __name__ == "__main__":
    mcp.run(transport="stdio")
