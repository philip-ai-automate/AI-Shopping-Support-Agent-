from typing import Optional


def normalize(payload: dict) -> Optional[dict]:
    """
    Parse a raw Meta webhook POST payload into a unified internal dict.
    Returns None for events that should be silently ignored (status updates,
    unknown types, empty content).

    Output keys:
      session_id, phone_number_id, customer_phone, customer_name,
      text, media_url, message_type, meta_message_id,
      action_type (None | "addcart" | "details" | "more"),
      action_product_id (str | None)
    """
    try:
        entry = (payload.get("entry") or [{}])[0]
        change = (entry.get("changes") or [{}])[0]
        value = change.get("value") or {}

        phone_number_id = (value.get("metadata") or {}).get("phone_number_id", "")

        # Delivery/read receipts — log-only, no AI action needed
        if value.get("statuses"):
            return None

        messages = value.get("messages")
        if not messages:
            return None

        msg = messages[0]
        msg_id = msg.get("id", "")
        customer_phone = msg.get("from", "")
        msg_type = msg.get("type", "")

        contacts = value.get("contacts") or [{}]
        customer_name = (contacts[0].get("profile") or {}).get("name", "")

        session_id = f"wa-meta-{phone_number_id}-{customer_phone}"

        text = None
        media_url = None
        action_type = None
        action_product_id = None

        if msg_type == "text":
            text = (msg.get("text") or {}).get("body", "").strip()

        elif msg_type == "interactive":
            interactive = msg.get("interactive") or {}
            itype = interactive.get("type", "")

            if itype == "button_reply":
                btn = interactive.get("button_reply") or {}
                btn_id = btn.get("id", "")
                btn_title = btn.get("title", "").strip()
                if btn_id.startswith("addcart_"):
                    action_product_id = btn_id.replace("addcart_", "")
                    action_type = "addcart"
                    text = f"I want to add product {action_product_id} to my cart"
                elif btn_id.startswith("details_"):
                    action_product_id = btn_id.replace("details_", "")
                    action_type = "details"
                    text = f"Tell me more about product {action_product_id}"
                elif btn_id == "more":
                    action_type = "more"
                    text = "Show me more options"
                else:
                    text = btn_title or btn_id

            elif itype == "list_reply":
                list_reply = interactive.get("list_reply") or {}
                selected_title = list_reply.get("title", "").strip()
                selected_id = (list_reply.get("id") or "").replace("prod_", "")
                action_type = "list_select"
                action_product_id = selected_id
                text = f"I'm interested in {selected_title} (product id: {selected_id})"

        elif msg_type in ("image", "video", "document", "audio"):
            media = msg.get(msg_type) or {}
            caption = (media.get("caption") or "").strip()
            media_url = media.get("link") or media.get("id") or ""
            text = caption if caption else f"[Customer sent a {msg_type}]"

        elif msg_type == "location":
            location = msg.get("location") or {}
            text = f"My location: {location.get('name', 'shared location')}"

        else:
            return None

        if not text:
            return None

        return {
            "session_id": session_id,
            "phone_number_id": phone_number_id,
            "customer_phone": customer_phone,
            "customer_name": customer_name,
            "text": text,
            "media_url": media_url,
            "message_type": msg_type,
            "meta_message_id": msg_id,
            "action_type": action_type,
            "action_product_id": action_product_id,
        }

    except Exception as e:
        print("⚠️ message_normalizer error:", e)
        return None
