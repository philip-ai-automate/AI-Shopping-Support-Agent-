from meta_sender import send_text, send_interactive_list, send_interactive_buttons


async def dispatch_response(
    phone_number_id: str,
    access_token: str,
    to: str,
    reply: str,
    products: list,
) -> None:
    """
    Send the AI reply and product recommendations to the customer.

    Decision logic (from design):
      no products  → plain text reply
      1 product    → plain text reply + quick-reply buttons (Add to Cart / View Details / More)
      2+ products  → plain text reply + interactive list (tappable rows)
    """
    if reply:
        await send_text(phone_number_id, access_token, to, reply)

    if not products:
        return

    if len(products) == 1:
        await send_interactive_buttons(phone_number_id, access_token, to, products[0])
    else:
        await send_interactive_list(phone_number_id, access_token, to, products)
