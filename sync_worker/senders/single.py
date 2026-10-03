from __future__ import annotations

from aiogram.types import ReplyParameters

import bot_engine

from ..core.media import extract_upload_metadata
from ..core.text import normalize_bot_html, normalize_pyro_html
from ..runtime.state import DeliveryClient


async def dynamic_send(
    client,
    msg_type,
    chat_id,
    file_ref,
    caption,
    parse_mode,
    reply_to_message_id=None,
    quote_data=None,
    progress=None,
    progress_args=None,
    thumbnail=None,
    has_spoiler=False,
    source_item=None,
):
    identity_client = client._client if isinstance(client, DeliveryClient) else client
    is_bot = bot_engine.is_bot_client(identity_client)
    method_name = "send_message" if msg_type == "text" else f"send_{msg_type}"
    method = getattr(client, method_name, None) or getattr(client, "send_document")
    kwargs = {"chat_id": chat_id}
    if msg_type != "sticker":
        kwargs["parse_mode"] = parse_mode
    if quote_data and reply_to_message_id:
        if is_bot:
            kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_to_message_id,
                quote=quote_data["text"],
                quote_position=quote_data.get("position"),
            )
        else:
            kwargs["reply_to_message_id"] = reply_to_message_id
            kwargs["quote_text"] = quote_data["text"]
            if quote_data.get("entities"):
                kwargs["quote_entities"] = quote_data["entities"]
    elif reply_to_message_id:
        kwargs["reply_to_message_id"] = reply_to_message_id
    if msg_type == "sticker":
        kwargs["sticker"] = file_ref
        if not is_bot:
            kwargs["emoji"] = ""
    elif msg_type != "text":
        kwargs["caption"] = normalize_bot_html(caption) if is_bot else normalize_pyro_html(caption)
        kwargs[msg_type if hasattr(client, method_name) else "document"] = file_ref
        kwargs.update(extract_upload_metadata(source_item, msg_type))
        if has_spoiler and msg_type in {"photo", "video", "animation"}:
            kwargs["has_spoiler"] = True
        if thumbnail is not None:
            if is_bot:
                kwargs["thumbnail"] = thumbnail
            else:
                kwargs["thumb"] = thumbnail
    else:
        kwargs["text"] = normalize_bot_html(caption) if is_bot else normalize_pyro_html(caption)
    if not is_bot and progress is not None:
        kwargs["progress"] = progress
        if progress_args:
            kwargs["progress_args"] = progress_args
    return await method(**kwargs)
