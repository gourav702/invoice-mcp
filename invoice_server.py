"""
Invoice MCP Server (POC) — talks to your AccountingSoftware web app
===================================================================

This MCP server does NOT touch the CSV files directly. Instead it calls your
existing app's HTTP API (http://localhost:3000/api/...), so your web app stays
the single source of truth and the stock-decrement logic lives in ONE place.

  ==> Your AccountingSoftware server MUST be running for this to work. <==

Tools in this phase:

  READ (safe — no changes):
    - list_invoices : list/filter invoice line-items
    - get_invoice   : fetch all line-items for one invoice number
    - list_items    : list inventory items (so you can see stock / pick an item)

  WRITE (CHANGES data — host shows a permission prompt):
    - create_invoice : create an invoice line-item for a real inventory item.
                       Your app checks stock and decrements it automatically.

Guardrails built in:
  * Input validation before any call (clear errors, never crashes).
  * A per-session cap so a runaway request can't flood your data.
  * An audit log (mcp_actions.log): time, tool, inputs, result.
  * Friendly errors — API problems are relayed in plain language, not stack traces.
"""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration — change here if your app runs elsewhere.
# ---------------------------------------------------------------------------
API_BASE = os.environ.get("INVOICE_API_BASE", "http://localhost:3000")
API_SECRET = os.environ.get("INVOICE_API_SECRET")  # must match the app's API_SECRET
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "mcp_actions.log")


def _auth_headers(extra: dict | None = None) -> dict:
    """Attach the shared secret (if set) so the hosted app accepts our writes."""
    headers = dict(extra or {})
    if API_SECRET:
        headers["X-API-Key"] = API_SECRET
    return headers

MAX_CREATES_PER_SESSION = 20
ALLOWED_STATUSES = ("unpaid", "paid", "overdue")

_creates_this_session = 0

mcp = FastMCP("invoices")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _log(tool: str, inputs: dict, status: str, detail: str = "") -> None:
    """Append one audit line. Logging must never break a tool."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\t"
                    f"{tool}\t{inputs}\t{status}\t{detail}\n")
    except Exception:
        pass


def _api_get(path: str):
    """GET JSON from the app. Returns (data, None) or (None, friendly_error)."""
    try:
        req = urllib.request.Request(API_BASE + path, headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.URLError as e:
        return None, (f"Could not reach your accounting app at {API_BASE}. "
                      f"Is it running? (start it with: python3 server.py)  [{e}]")
    except Exception as e:
        return None, f"Unexpected error reading from the app: {e}"


def _api_post(path: str, payload: dict):
    """POST JSON to the app. Returns (data, None) or (None, friendly_error)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API_BASE + path, data=data, method="POST",
                                 headers=_auth_headers({"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        # The app sends a helpful {"error": "..."} body (e.g. "only 3 available").
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", str(e))
        except Exception:
            msg = str(e)
        return None, msg
    except urllib.error.URLError as e:
        return None, (f"Could not reach your accounting app at {API_BASE}. "
                      f"Is it running?  [{e}]")
    except Exception as e:
        return None, f"Unexpected error writing to the app: {e}"


def _valid_date(text: str) -> bool:
    if not text:
        return True  # due_date is optional
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# READ TOOLS
# ---------------------------------------------------------------------------
@mcp.tool()
def list_invoices(
    status: str | None = None,
    customer: str | None = None,
    item: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
) -> dict:
    """READ-ONLY. List invoice line-items, with optional filters. Changes nothing.

    Each invoice row is one item sold under an invoice number.
    Filters (all optional, combine freely):
      status      : unpaid / paid / overdue
      customer    : partial, case-insensitive match on customer name
      item        : partial, case-insensitive match on item name
      min_amount  : only rows with unit price >= this number
      max_amount  : only rows with unit price <= this number
    """
    rows, err = _api_get("/api/invoices")
    if err:
        _log("list_invoices", {"status": status}, "error", err)
        return {"error": err}
    out = []
    for r in rows:
        if status and r.get("status", "").lower() != status.lower():
            continue
        if customer and customer.lower() not in (r.get("customerName") or "").lower():
            continue
        if item and item.lower() not in (r.get("itemName") or "").lower():
            continue
        price = float(r.get("price") or 0)
        if min_amount is not None and price < min_amount:
            continue
        if max_amount is not None and price > max_amount:
            continue
        out.append(r)
    _log("list_invoices",
         {"status": status, "customer": customer, "item": item,
          "min_amount": min_amount, "max_amount": max_amount},
         "ok", f"{len(out)} matched")
    return {"count": len(out), "invoices": out}


@mcp.tool()
def get_invoice(number: str) -> dict:
    """READ-ONLY. Fetch every line-item belonging to one invoice number.

    An invoice number can have several items, so this returns a list of rows
    plus a total. Returns a clear message if the number isn't found.
    """
    rows, err = _api_get("/api/invoices")
    if err:
        _log("get_invoice", {"number": number}, "error", err)
        return {"error": err}
    matches = [r for r in rows if str(r.get("number", "")) == str(number)]
    if not matches:
        _log("get_invoice", {"number": number}, "not_found")
        return {"error": f"No invoice found with number '{number}'."}
    total = sum(float(r.get("price") or 0) * int(r.get("qty") or 0) for r in matches)
    _log("get_invoice", {"number": number}, "ok", f"{len(matches)} line(s)")
    return {"number": number, "line_count": len(matches),
            "invoice_total": total, "lines": matches}


@mcp.tool()
def list_items(name: str | None = None) -> dict:
    """READ-ONLY. List inventory items and their stock levels. Changes nothing.

    Optional 'name' does a partial, case-insensitive match. Use this to see what
    can be sold (and how much stock is left) before creating an invoice.
    """
    items, err = _api_get("/api/items")
    if err:
        _log("list_items", {"name": name}, "error", err)
        return {"error": err}
    if name:
        items = [i for i in items if name.lower() in (i.get("name") or "").lower()]
    _log("list_items", {"name": name}, "ok", f"{len(items)} matched")
    return {"count": len(items), "items": items}


# ---------------------------------------------------------------------------
# WRITE TOOL
# ---------------------------------------------------------------------------
@mcp.tool()
def create_invoice(
    number: str,
    item_name: str,
    qty: int,
    customer_name: str | None = None,
    price: float | None = None,
    customer_email: str | None = None,
    due_date: str | None = None,
    status: str = "unpaid",
    notes: str | None = None,
) -> dict:
    """WRITES — creates a new invoice line-item and DECREMENTS that item's stock.

    Your accounting app performs the stock check and decrement, so this can fail
    if there isn't enough stock (you'll get a clear message).

    Required:
      number    : the invoice number (text, e.g. INV-1001)
      item_name : name of an existing inventory item (must match one item)
      qty       : how many units to sell (whole number > 0)
    Optional:
      customer_name, customer_email
      price     : unit price; defaults to the item's catalogue price if omitted
      due_date  : YYYY-MM-DD
      status    : unpaid / paid / overdue (default unpaid)
      notes
    """
    global _creates_this_session
    inputs = {"number": number, "item_name": item_name, "qty": qty,
              "customer_name": customer_name, "status": status}

    # --- Validation ---
    if not number or not str(number).strip():
        _log("create_invoice", inputs, "rejected", "empty number")
        return {"error": "An invoice number is required."}
    try:
        qty = int(qty)
    except (ValueError, TypeError):
        _log("create_invoice", inputs, "rejected", "qty not int")
        return {"error": "qty must be a whole number."}
    if qty <= 0:
        _log("create_invoice", inputs, "rejected", "qty<=0")
        return {"error": "qty must be greater than 0."}
    if status not in ALLOWED_STATUSES:
        _log("create_invoice", inputs, "rejected", "bad status")
        return {"error": f"status must be one of {list(ALLOWED_STATUSES)}."}
    if not _valid_date(due_date or ""):
        _log("create_invoice", inputs, "rejected", "bad due_date")
        return {"error": "due_date must be a valid date in YYYY-MM-DD format."}
    if price is not None:
        try:
            price = float(price)
        except (ValueError, TypeError):
            _log("create_invoice", inputs, "rejected", "price not number")
            return {"error": "price must be a number."}
        if price < 0:
            _log("create_invoice", inputs, "rejected", "price<0")
            return {"error": "price cannot be negative."}

    # --- Safety cap ---
    if _creates_this_session >= MAX_CREATES_PER_SESSION:
        _log("create_invoice", inputs, "rejected", "session cap")
        return {"error": f"Safety cap reached: at most {MAX_CREATES_PER_SESSION} "
                         f"invoices per session. Restart the server to reset."}

    # --- Resolve the item by name (must match exactly one) ---
    items, err = _api_get("/api/items")
    if err:
        _log("create_invoice", inputs, "error", err)
        return {"error": err}
    matches = [i for i in items if (i.get("name") or "").lower() == item_name.lower()]
    if not matches:
        near = [i["name"] for i in items if item_name.lower() in (i.get("name") or "").lower()]
        hint = f" Did you mean: {near}?" if near else ""
        _log("create_invoice", inputs, "rejected", "item not found")
        return {"error": f"No inventory item named '{item_name}'.{hint} "
                         f"Use list_items to see available items."}
    if len(matches) > 1:
        _log("create_invoice", inputs, "rejected", "ambiguous item")
        return {"error": f"More than one item is named '{item_name}'. "
                         f"This POC can't tell them apart by name alone."}
    item = matches[0]

    # Friendly local stock check (the app also enforces this).
    if qty > int(item.get("qty") or 0):
        _log("create_invoice", inputs, "rejected", "insufficient stock")
        return {"error": f"Only {item['qty']} of '{item['name']}' in stock; "
                         f"you asked for {qty}."}

    if price is None:
        price = float(item.get("price") or 0)

    # --- Create via the app's API (it does the atomic write + stock decrement) ---
    payload = {
        "number": str(number).strip(),
        "itemId": item["id"],
        "qty": qty,
        "price": price,
        "customerName": (customer_name or "").strip(),
        "customerEmail": (customer_email or "").strip(),
        "dueDate": (due_date or "").strip(),
        "status": status,
        "notes": (notes or "").strip(),
    }
    result, err = _api_post("/api/invoices", payload)
    if err:
        _log("create_invoice", inputs, "error", err)
        return {"error": err}

    _creates_this_session += 1
    _log("create_invoice", inputs, "ok", f"{number} / {item['name']} x{qty}")
    return {
        "message": "Invoice created and stock updated.",
        "invoice": result.get("invoice"),
        "item_after": result.get("item"),
    }


if __name__ == "__main__":
    # Local (Claude Desktop on your Mac) uses stdio. A hosted deploy sets
    # MCP_TRANSPORT=http to expose an HTTPS endpoint other machines can reach.
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "http":
        from mcp.server.transport_security import TransportSecuritySettings
        mcp.settings.host = os.environ.get("HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("PORT", "8000"))
        # Hosted behind a proxy (Render): the public hostname isn't localhost,
        # so turn off the localhost-only DNS-rebinding guard. Writes are still
        # protected by the API secret between this server and the app.
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False)
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
