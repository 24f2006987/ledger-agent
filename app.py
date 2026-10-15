
import os
import re
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Appliances Ledger Agent")

BASE_URL = os.environ.get("LEDGER_DATA_URL", "").strip()

RATES = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.07"),
    "INR": Decimal("0.01187"),
}

IST = ZoneInfo("Asia/Kolkata")
cache = None


class Question(BaseModel):
    question: str


def fetch_json(path, page=None):
    if not BASE_URL:
        raise RuntimeError(
            "LEDGER_DATA_URL is not configured. Set it in PowerShell."
        )

    parsed = urlparse(BASE_URL)
    params = parse_qs(parsed.query)
    params["path"] = [path]
    params.pop("page", None)

    if page is not None:
        params["page"] = [str(page)]

    url = urlunparse(
        parsed._replace(query=urlencode(params, doseq=True))
    )

    response = requests.get(url, timeout=12)
    response.raise_for_status()
    return response.json()


def parse_datetime(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_ledger():
    global cache

    if cache is not None:
        return cache

    first = fetch_json("/orders", 1)
    total_pages = int(first.get("pages", 1))
    pages_data = [first]

    # Fetch all remaining pages concurrently.
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [
            executor.submit(fetch_json, "/orders", page)
            for page in range(2, total_pages + 1)
        ]

        for future in as_completed(futures):
            pages_data.append(future.result())

    # For duplicate order IDs, keep the most recently updated row.
    orders_by_id = {}

    for page_data in pages_data:
        for order in page_data.get("orders", []):
            order_id = order.get("id")

            if not order_id:
                continue

            previous = orders_by_id.get(order_id)

            if previous is None:
                orders_by_id[order_id] = order
                continue

            if parse_datetime(order["updated_at"]) > parse_datetime(
                previous["updated_at"]
            ):
                orders_by_id[order_id] = order

    rates_data = fetch_json("/rates")
    rates = rates_data.get("usd_per_unit", RATES)

    cache = {
        "orders": list(orders_by_id.values()),
        "rates": {
            key: Decimal(str(value))
            for key, value in rates.items()
        },
    }

    return cache


def usd_amount(order, rates):
    amount = Decimal(str(order.get("amount", 0)))
    currency = order.get("currency", "USD")
    rate = rates.get(currency, Decimal("1"))

    return (amount * rate).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def money(value):
    return str(
        Decimal(value).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def answer_question(question, ledger):
    q = question.lower()
    orders = ledger["orders"]
    rates = ledger["rates"]

    # Orders already contain only the latest row for each ID.
    all_orders = list(orders)
    paid = [o for o in all_orders if o.get("status") == "paid"]

    # Direct order lookup.
    order_match = re.search(r"\bord-\d+\b", q, re.I)

    if order_match:
        order_id = order_match.group(0).upper()
        order = next(
            (
                o for o in all_orders
                if o.get("id", "").upper() == order_id
            ),
            None,
        )

        if order is None:
            return {"answer": None}

        return {
            "answer": {
                "id": order.get("id"),
                "customer": order.get("customer"),
                "product": order.get("product"),
                "status": order.get("status"),
                "amount_usd": money(usd_amount(order, rates)),
            }
        }

    # Filter by product name.
    products = sorted(
        {o.get("product", "") for o in all_orders if o.get("product")},
        key=len,
        reverse=True,
    )

    selected_product = next(
        (p for p in products if p.lower() in q),
        None,
    )

    if selected_product:
        paid = [
            o for o in paid
            if o.get("product") == selected_product
        ]

    # Filter by region.
    regions = {
        o.get("region", "")
        for o in all_orders
        if o.get("region")
    }

    selected_region = next(
        (r for r in regions if r.lower() in q),
        None,
    )

    if selected_region:
        paid = [
            o for o in paid
            if o.get("region") == selected_region
        ]

    # Filter by customer ID.
    customer_match = re.search(r"\bc\d{4}\b", q, re.I)

    if customer_match:
        customer_id = customer_match.group(0).upper()
        paid = [
            o for o in paid
            if o.get("customer", "").upper() == customer_id
        ]

    # Filter by date using Asia/Kolkata business dates.
    date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", q)

    if date_match:
        target_date = date_match.group(1)

        paid = [
            o for o in paid
            if parse_datetime(o["created_at"])
            .astimezone(IST).date().isoformat() == target_date
        ]

    # Order-count questions.
    if any(
        word in q
        for word in (
            "how many",
            "count",
            "number of orders",
            "quantity of orders",
        )
    ):
        return {"answer": len(paid)}

    # Total revenue in USD.
    total = sum(
        (usd_amount(o, rates) for o in paid),
        Decimal("0.00"),
    )

    # Average order amount.
    if "average" in q or "mean" in q:
        average = total / len(paid) if paid else Decimal("0")
        return {"answer": money(average)}

    # Highest-revenue product.
    if any(
        word in q
        for word in ("top product", "highest", "most revenue",
                     "best-selling", "best selling")
    ):
        totals = {}

        for order in paid:
            product = order.get("product", "")
            totals[product] = (
                totals.get(product, Decimal("0.00"))
                + usd_amount(order, rates)
            )

        if totals:
            return {
                "answer": max(totals, key=totals.get)
            }

    # Customer with the highest revenue.
    if "customer" in q and any(
        word in q for word in ("highest", "most", "top", "revenue")
    ):
        customer_totals = {}

        for order in paid:
            customer = order.get("customer", "")
            customer_totals[customer] = (
                customer_totals.get(customer, Decimal("0.00"))
                + usd_amount(order, rates)
            )

        if customer_totals:
            return {
                "answer": max(
                    customer_totals,
                    key=customer_totals.get,
                )
            }

    return {"answer": money(total)}


@app.get("/")
def home():
    return {"status": "ok", "service": "Acme Appliances Ledger Agent"}


@app.post("/")
@app.post("/answer")
def answer(request: Question):
    try:
        ledger = load_ledger()
        return answer_question(request.question, ledger)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Ledger data error: {str(exc)}",
        )
