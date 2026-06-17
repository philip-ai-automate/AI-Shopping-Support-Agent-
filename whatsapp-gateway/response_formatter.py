from meta_sender import send_text, send_interactive_list
from currency import fmt_ngn


async def dispatch_response(
    phone_number_id: str,
    access_token: str,
    to: str,
    reply: str,
    products: list,
    session_id: str = "",
) -> None:
    """
    Send the AI reply and any product recommendations to the customer.

    Flow:
      no products  → send the AI text reply only
      1+ products  → send AI text reply, then show all products as an
                     interactive list ("Products for you" / "View Options").
                     When the customer selects a row, handle_list_select
                     in interactive_handler.py sends the rich detail card.
    """
    list_products = [
        {
            "product_id": str(p.get("product_id") or p.get("id") or ""),
            "name":       p.get("name") or p.get("product_name") or "",
            "price":      fmt_ngn(str(p.get("price") or "")),
            "in_stock":   p.get("in_stock", True),
            "related":    p.get("related", False),
        }
        for p in products
    ] if products else []

    if list_products:
        # Products present — send only the interactive list, no text reply.
        # Sending both causes the customer to see the product list twice
        # (AI text + "Products for you" cards) which looks broken.
        await send_interactive_list(phone_number_id, access_token, to, list_products)
    elif reply:
        await send_text(phone_number_id, access_token, to, reply)
