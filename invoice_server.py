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
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage

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

# --- Email settings (for sending payment-request emails) ---
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")            # e.g. you@gmail.com
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")    # e.g. a Gmail App Password
FROM_EMAIL = os.environ.get("FROM_EMAIL") or SMTP_USER

_creates_this_session = 0


def _email_configured() -> bool:
    return bool(SMTP_USER and SMTP_PASSWORD)


def _looks_like_email(addr: str) -> bool:
    return "@" in addr and "." in addr.split("@")[-1] and len(addr) >= 5


def _send_email(to_addr: str, subject: str, body: str):
    """Send a plain-text email. Returns (ok, error_message)."""
    if not _email_configured():
        return False, ("Email isn't set up on the server yet. The admin needs to set "
                       "SMTP_USER and SMTP_PASSWORD (e.g. a Gmail address + App Password).")
    msg = EmailMessage()
    msg["From"] = FROM_EMAIL or SMTP_USER
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as srv:
                srv.login(SMTP_USER, SMTP_PASSWORD)
                srv.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as srv:
                srv.starttls(context=ssl.create_default_context())
                srv.login(SMTP_USER, SMTP_PASSWORD)
                srv.send_message(msg)
        return True, None
    except Exception as e:
        return False, f"Could not send the email: {e}"

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


def _api_put(path: str, payload: dict):
    """PUT JSON to the app. Returns (data, None) or (None, friendly_error)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API_BASE + path, data=data, method="PUT",
                                 headers=_auth_headers({"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", str(e))
        except Exception:
            msg = str(e)
        return None, msg
    except urllib.error.URLError as e:
        return None, (f"Could not reach your accounting app at {API_BASE}. Is it running?  [{e}]")
    except Exception as e:
        return None, f"Unexpected error writing to the app: {e}"


def _api_delete(path: str):
    """DELETE request to the app. Returns (data, None) or (None, friendly_error)."""
    req = urllib.request.Request(API_BASE + path, method="DELETE", headers=_auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", str(e))
        except Exception:
            msg = str(e)
        return None, msg
    except urllib.error.URLError as e:
        return None, (f"Could not reach your accounting app at {API_BASE}. Is it running?  [{e}]")
    except Exception as e:
        return None, f"Unexpected error deleting from the app: {e}"


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
    min_qty: int | None = None,
    max_qty: int | None = None,
) -> dict:
    """READ-ONLY. List invoice line-items, with optional filters. Changes nothing.

    Each invoice row is one item sold under an invoice number.
    Filters (all optional, combine freely):
      status      : unpaid / paid / overdue
      customer    : partial, case-insensitive match on customer name
      item        : partial, case-insensitive match on item name
      min_amount  : only rows with unit price >= this number
      max_amount  : only rows with unit price <= this number
      min_qty     : only rows with quantity >= this number
      max_qty     : only rows with quantity <= this number
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
        qty = int(r.get("qty") or 0)
        if min_amount is not None and price < min_amount:
            continue
        if max_amount is not None and price > max_amount:
            continue
        if min_qty is not None and qty < min_qty:
            continue
        if max_qty is not None and qty > max_qty:
            continue
        out.append(r)
    _log("list_invoices",
         {"status": status, "customer": customer, "item": item,
          "min_amount": min_amount, "max_amount": max_amount,
          "min_qty": min_qty, "max_qty": max_qty},
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
def list_items(name: str | None = None,
               min_qty: int | None = None, max_qty: int | None = None,
               min_price: float | None = None, max_price: float | None = None) -> dict:
    """READ-ONLY. List inventory items and their stock levels, with optional filters.

    Filters (all optional, combine freely):
      name       : partial, case-insensitive match on item name
      min_qty    : only items with stock >= this number (e.g. min_qty=1 = in stock)
      max_qty    : only items with stock <= this number (e.g. max_qty=0 = out of stock)
      min_price  : only items priced >= this
      max_price  : only items priced <= this
    """
    items, err = _api_get("/api/items")
    if err:
        _log("list_items", {"name": name}, "error", err)
        return {"error": err}
    out = []
    for i in items:
        if name and name.lower() not in (i.get("name") or "").lower():
            continue
        qty = int(i.get("qty") or 0)
        price = float(i.get("price") or 0)
        if min_qty is not None and qty < min_qty:
            continue
        if max_qty is not None and qty > max_qty:
            continue
        if min_price is not None and price < min_price:
            continue
        if max_price is not None and price > max_price:
            continue
        out.append(i)
    _log("list_items", {"name": name, "min_qty": min_qty, "max_qty": max_qty,
                        "min_price": min_price, "max_price": max_price},
         "ok", f"{len(out)} matched")
    return {"count": len(out), "items": out}


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

    You can call this in two ways:
      1) The user gives the details in chat, or
      2) The user UPLOADS an invoice (PDF/image/scan) — read the document, extract
         the fields below, and call this tool with them.

    IMPORTANT: If any REQUIRED field (number, item_name, qty) is missing or you are
    not confident you read it correctly from an uploaded document, ASK the user to
    confirm or provide it instead of guessing. Do not invent values.

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


@mcp.tool()
def create_item(name: str, qty: int, price: float) -> dict:
    """WRITES — adds a NEW inventory item (so it can be sold on invoices).

    Required:
      name  : item name (non-empty)
      qty   : starting stock, a whole number >= 0
      price : catalogue unit price, a number >= 0
    Returns the created item including its new id.
    """
    global _creates_this_session
    inputs = {"name": name, "qty": qty, "price": price}
    if not name or not str(name).strip():
        _log("create_item", inputs, "rejected", "empty name")
        return {"error": "Item name must not be empty."}
    try:
        qty = int(qty)
    except (ValueError, TypeError):
        return {"error": "qty must be a whole number."}
    if qty < 0:
        return {"error": "qty cannot be negative."}
    try:
        price = float(price)
    except (ValueError, TypeError):
        return {"error": "price must be a number."}
    if price < 0:
        return {"error": "price cannot be negative."}
    if _creates_this_session >= MAX_CREATES_PER_SESSION:
        return {"error": f"Safety cap reached: at most {MAX_CREATES_PER_SESSION} "
                         f"creations per session. Restart the server to reset."}
    result, err = _api_post("/api/items", {"name": name.strip(), "qty": qty, "price": price})
    if err:
        _log("create_item", inputs, "error", err)
        return {"error": err}
    _creates_this_session += 1
    _log("create_item", inputs, "ok", result.get("name"))
    return {"message": "Item created.", "item": result}


@mcp.tool()
def update_item(item_id: int, name: str | None = None,
                qty: int | None = None, price: float | None = None) -> dict:
    """WRITES — updates an existing inventory item's name, stock, and/or price.

    Required:
      item_id : the id of the item to change (see list_items)
    Optional (give at least one):
      name  : new name (non-empty)
      qty   : new stock level, a whole number >= 0
      price : new unit price, a number >= 0
    Only the fields you provide are changed.
    """
    inputs = {"item_id": item_id, "name": name, "qty": qty, "price": price}
    try:
        item_id = int(item_id)
    except (ValueError, TypeError):
        return {"error": "item_id must be a whole number."}
    payload: dict = {"id": item_id}
    if name is not None:
        if not str(name).strip():
            return {"error": "name cannot be empty."}
        payload["name"] = str(name).strip()
    if qty is not None:
        try:
            payload["qty"] = int(qty)
        except (ValueError, TypeError):
            return {"error": "qty must be a whole number."}
        if payload["qty"] < 0:
            return {"error": "qty cannot be negative."}
    if price is not None:
        try:
            payload["price"] = float(price)
        except (ValueError, TypeError):
            return {"error": "price must be a number."}
        if payload["price"] < 0:
            return {"error": "price cannot be negative."}
    if len(payload) == 1:
        return {"error": "Nothing to update — provide a new name, qty, and/or price."}
    result, err = _api_put("/api/items", payload)
    if err:
        _log("update_item", inputs, "error", err)
        return {"error": err}
    _log("update_item", inputs, "ok")
    return {"message": "Item updated.", "item": result}


@mcp.tool()
def update_invoice(number: str, customer_name: str | None = None,
                   customer_email: str | None = None, due_date: str | None = None,
                   status: str | None = None, notes: str | None = None,
                   price: float | None = None) -> dict:
    """WRITES — updates an existing invoice's details (found by its number).

    NOTE: this changes invoice DETAILS only — not the item or quantity — so stock
    stays correct. The invoice number must match exactly one invoice.

    Required:
      number : the invoice number to update (e.g. INV-1001)
    Optional (give at least one):
      customer_name, customer_email, notes
      due_date : YYYY-MM-DD
      status   : unpaid / paid / overdue
      price    : unit price, a number >= 0
    """
    inputs = {"number": number, "status": status}
    if not number or not str(number).strip():
        return {"error": "An invoice number is required."}
    payload: dict = {"number": str(number).strip()}
    if customer_name is not None:
        payload["customerName"] = str(customer_name).strip()
    if customer_email is not None:
        payload["customerEmail"] = str(customer_email).strip()
    if notes is not None:
        payload["notes"] = str(notes).strip()
    if due_date is not None:
        if not _valid_date(due_date):
            return {"error": "due_date must be a valid date in YYYY-MM-DD format."}
        payload["dueDate"] = due_date
    if status is not None:
        if status not in ALLOWED_STATUSES:
            return {"error": f"status must be one of {list(ALLOWED_STATUSES)}."}
        payload["status"] = status
    if price is not None:
        try:
            payload["price"] = float(price)
        except (ValueError, TypeError):
            return {"error": "price must be a number."}
        if payload["price"] < 0:
            return {"error": "price cannot be negative."}
    if len(payload) == 1:
        return {"error": "Nothing to update — provide at least one field to change."}
    result, err = _api_put("/api/invoices", payload)
    if err:
        _log("update_invoice", inputs, "error", err)
        return {"error": err}
    _log("update_invoice", inputs, "ok")
    return {"message": "Invoice updated.", "invoice": result}


@mcp.tool()
def send_invoice_email(number: str, to_email: str | None = None,
                       message: str | None = None) -> dict:
    """SENDS AN EMAIL — emails a payment request to the customer for an invoice.

    Looks up the invoice by number, works out the total amount due, and emails the
    customer. By default the body says "Please pay this <amount>", but you can pass
    your own message.

    Required:
      number   : the invoice number to send (must match exactly one invoice)
    Optional:
      to_email : recipient; defaults to the invoice's saved customer email
      message  : custom message text; defaults to "Please pay this <amount>."
    """
    inputs = {"number": number, "to_email": to_email}
    if not number or not str(number).strip():
        return {"error": "An invoice number is required."}
    rows, err = _api_get("/api/invoices")
    if err:
        _log("send_invoice_email", inputs, "error", err)
        return {"error": err}
    matches = [r for r in rows if str(r.get("number", "")) == str(number)]
    if not matches:
        _log("send_invoice_email", inputs, "not_found")
        return {"error": f"No invoice found with number '{number}'."}

    total = sum(float(r.get("price") or 0) * int(r.get("qty") or 0) for r in matches)
    recipient = (to_email or matches[0].get("customerEmail") or "").strip()
    if not recipient:
        _log("send_invoice_email", inputs, "rejected", "no recipient")
        return {"error": f"Invoice {number} has no customer email saved. "
                         f"Provide to_email, or add the customer's email to the invoice."}
    if not _looks_like_email(recipient):
        return {"error": f"'{recipient}' doesn't look like a valid email address."}

    body_line = message.strip() if message else f"Please pay this {total}."
    items_str = ", ".join(f"{r['qty']} x {r['itemName']}" for r in matches)
    full_body = (f"{body_line}\n\n"
                 f"Invoice number: {number}\n"
                 f"Items: {items_str}\n"
                 f"Amount due: {total}\n")
    ok, send_err = _send_email(recipient, f"Invoice {number} — payment request", full_body)
    if not ok:
        _log("send_invoice_email", inputs, "error", send_err)
        return {"error": send_err}
    _log("send_invoice_email", inputs, "ok", f"{recipient} / {total}")
    return {"message": f"Payment-request email sent to {recipient}.",
            "invoice": number, "amount_due": total}


@mcp.tool()
def delete_invoice(number: str, restore_stock: bool = True) -> dict:
    """WRITES / DESTRUCTIVE — permanently deletes an invoice (cancels it).

    SAFETY: only deletes when the invoice number matches EXACTLY ONE invoice. If a
    number is shared by several rows, it refuses (so it can never mass-delete).

    By default the sold units are RETURNED to stock (restore_stock=True), treating
    the deletion as a cancellation. Set restore_stock=False to delete without
    changing stock.

    Required:
      number        : the invoice number to delete (must be unique)
    Optional:
      restore_stock : add the sold quantity back to the item's stock (default True)
    """
    inputs = {"number": number, "restore_stock": restore_stock}
    if not number or not str(number).strip():
        return {"error": "An invoice number is required."}
    number = str(number).strip()

    rows, err = _api_get("/api/invoices")
    if err:
        _log("delete_invoice", inputs, "error", err)
        return {"error": err}
    matches = [r for r in rows if str(r.get("number", "")) == number]
    if not matches:
        _log("delete_invoice", inputs, "not_found")
        return {"error": f"No invoice found with number '{number}'."}
    if len(matches) > 1:
        _log("delete_invoice", inputs, "rejected", "ambiguous")
        return {"error": f"{len(matches)} invoices share number '{number}'. For safety "
                         f"this tool only deletes when the number is unique."}

    inv = matches[0]
    # Delete the single matching invoice.
    result, err = _api_delete(f"/api/invoices?number={urllib.parse.quote(number)}")
    if err:
        _log("delete_invoice", inputs, "error", err)
        return {"error": err}

    # Optionally return the sold units to stock.
    restored = None
    if restore_stock:
        items, ierr = _api_get("/api/items")
        if not ierr:
            it = next((i for i in items
                       if (i.get("name") or "").lower() == (inv.get("itemName") or "").lower()), None)
            if it:
                new_qty = int(it.get("qty") or 0) + int(inv.get("qty") or 0)
                _, perr = _api_put("/api/items", {"id": it["id"], "qty": new_qty})
                if not perr:
                    restored = {"item": it["name"], "added_back": inv.get("qty"), "new_qty": new_qty}

    _log("delete_invoice", inputs, "ok", f"deleted {number}; restored={bool(restored)}")
    return {"message": f"Invoice {number} deleted.",
            "deleted_invoice": inv,
            "stock_restored": restored}


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
