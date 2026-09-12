from __future__ import annotations

from pyrogram.enums import ParseMode
from pyrogram.types import InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo

import bot_engine
import database as db
from services.sync_services import build_link_rewrite_context, rewrite_message_links

from .text import prepend_source_header_html


PYRO_MEDIA_CLS = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "audio": InputMediaAudio,
    "document": InputMediaDocument,
}


async def rewrite_media_group_captions(source_id, target_id, group, source_username_override=None, include_external_source_header: bool = False):
    link_context = await build_link_rewrite_context(
        bot_engine.aiogram_bot,
        source_id,
        target_id,
        source_username_override=source_username_override,
    )
    captions = []
    changed = False
    total_rewrites = 0

    # 找到第一个有 caption 的媒体项索引
    first_caption_index = None
    for index, item in enumerate(group):
        if item.caption and item.caption.html:
            first_caption_index = index
            break
    
    # 如果没有任何媒体有 caption，则在第一个媒体上添加转发信息
    should_add_to_first = first_caption_index is None

    for index, item in enumerate(group):
        # 获取原始 caption
        raw_caption = item.caption.html if item.caption else ""
        _, filtered_caption = await db.apply_message_filters(raw_caption, True, "")
        if filtered_caption != raw_caption:
            changed = True
        
        # 决定是否在当前媒体上添加外部来源前缀
        # 规则：有文字的在文字上加（第一个有文字的），没有文字的在第一个媒体上加
        should_add_header = include_external_source_header and (
            (should_add_to_first and index == 0) or  # 没有任何文字时，在第一个媒体上加
            (not should_add_to_first and index == first_caption_index)  # 有文字时，在第一个有文字的媒体上加
        )
        caption_with_header = prepend_source_header_html(filtered_caption, item, enabled=should_add_header)
        
        # 如果添加了前缀，标记为已改变
        if caption_with_header != raw_caption:
            changed = True
        
        # 链接改写
        rewritten_caption, rewrite_count = await rewrite_message_links(caption_with_header, source_id, link_context)
        
        # 如果链接改写后有变化，也标记为已改变
        if rewritten_caption != caption_with_header:
            changed = True
        
        total_rewrites += rewrite_count
        captions.append(rewritten_caption)

    return captions, changed, total_rewrites
