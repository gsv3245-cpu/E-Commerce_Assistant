"""Evaluation cases built from the current product, order, and policy CSVs."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import tools


@dataclass
class Case:
    id: str
    kind: str
    query: str
    customer: Optional[str] = None
    expected_tools: List[str] = field(default_factory=list)
    optional_tools: List[str] = field(default_factory=list)
    order: Optional[str] = None
    constraints: Optional[Dict[str, Any]] = None
    expect_empty: bool = False
    decision: Optional[str] = None
    expect_return: Optional[bool] = None
    must_contain: List[str] = field(default_factory=list)
    must_not_contain: List[str] = field(default_factory=list)
    refs: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    ref_answer: Optional[str] = None
    tags: List[str] = field(default_factory=list)


def _discovery_cases() -> List[Case]:
    cases = []
    for index, category in enumerate(tools._valid_categories(), start=1):
        cases.append(Case(
            id=f"D{index:02d}",
            kind="discovery",
            query=f"Show me products in {category}",
            expected_tools=["search_products"],
            constraints={"category": category},
            must_contain=[category],
            refs=[("search_products", {"category": category, "limit": 20})],
            tags=["category", "data-driven"],
        ))

    products = tools.load_products()
    categories = tools._valid_categories()
    cases.append(Case(
        id=f"D{len(cases) + 1:02d}",
        kind="discovery",
        query="What product categories do you sell?",
        expected_tools=["list_categories"],
        must_contain=[categories[0]],
        refs=[("list_categories", {})],
        tags=["catalogue"],
    ))
    cases.append(Case(
        id=f"D{len(cases) + 1:02d}",
        kind="discovery",
        query=f"Find products priced above {max(p['price'] for p in products)} rupees",
        expected_tools=["search_products"],
        constraints={},
        expect_empty=True,
        refs=[("search_products", {"min_price": max(p["price"] for p in products) + 1})],
        tags=["empty result"],
    ))
    sample = products[0]
    cases.append(Case(
        id=f"D{len(cases) + 1:02d}",
        kind="discovery",
        query=f"Tell me about product {sample['product_id']}",
        expected_tools=["get_product_details"],
        must_contain=[sample["product_name"]],
        refs=[("get_product_details", {"product": sample["product_id"]})],
        tags=["product lookup"],
    ))
    return cases


def _service_cases() -> List[Case]:
    orders = tools.load_orders()
    customers = tools.load_customers()
    cases = []
    first_order = orders[0]
    customer_orders = [order for order in orders if order["customer_id"] == first_order["customer_id"]]
    cases.append(Case(
        id="S01",
        kind="service",
        query="Show me all orders on my account",
        customer=first_order["customer_id"],
        expected_tools=["list_my_orders"],
        must_contain=[customer_orders[0]["order_id"]],
        refs=[("list_my_orders", {})],
        tags=["order list"],
    ))

    for index, order in enumerate(orders[:30], start=2):
        cases.append(Case(
            id=f"S{index:02d}",
            kind="service",
            query=f"What is the status of order {order['order_id']}?",
            customer=order["customer_id"],
            expected_tools=["get_order_status"],
            order=order["order_id"],
            must_contain=[order["order_id"], order["order_status"]],
            refs=[("get_order_status", {"order_id": order["order_id"]})],
            tags=["order status", "data-driven"],
        ))

    for index, category in enumerate(tools._valid_categories()[:3], start=32):
        cases.append(Case(
            id=f"S{index:02d}",
            kind="service",
            query=f"What is the return policy for {category}?",
            expected_tools=["get_return_policy"],
            must_contain=[category],
            refs=[("get_return_policy", {"category": category})],
            tags=["policy", "synthetic policy"],
        ))

    order = orders[0]
    other_customer = next(customer_id for customer_id in customers if customer_id != order["customer_id"])
    cases.append(Case(
        id="S35",
        kind="service",
        query=f"Where is order {order['order_id']}?",
        customer=other_customer,
        expected_tools=["get_order_status"],
        order=order["order_id"],
        must_contain=["could not find/couldn't find/can't find/cannot find/not found"],
        must_not_contain=[order["product_id"], order["order_status"]],
        refs=[("get_order_status", {"order_id": order["order_id"]})],
        tags=["access control"],
    ))
    return cases


DISCOVERY_CASES = _discovery_cases()
SERVICE_CASES = _service_cases()
ALL_CASES = DISCOVERY_CASES + SERVICE_CASES
