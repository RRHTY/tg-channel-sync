"""Cheap request checks and the shared Telegram Desktop export reader."""
import json
import math
from pathlib import Path


def validate_sync_request(mode, sender, target_type, source_id, target_id, delay, start_id, end_id, window):
    if mode not in {'api', 'clone', 'json'}:
        raise ValueError('请选择有效的同步模式')
    if sender not in {'bot', 'user'} or target_type not in {'channel', 'saved'}:
        raise ValueError('发送身份或目标类型无效')
    if mode != 'json' and not source_id.strip():
        raise ValueError('请填写源频道')
    if target_type != 'saved' and not target_id.strip():
        raise ValueError('请填写目标频道')
    if not math.isfinite(delay) or delay < 0.5:
        raise ValueError('单条延时须为至少 0.5 秒的有效数字')
    if start_id < 0 or end_id < 0 or (end_id and start_id > end_id):
        raise ValueError('消息 ID 不可为负，结束 ID 不可小于起始 ID；留空表示不限')
    if window < 1:
        raise ValueError('媒体组合并窗口须至少为 1 秒')


def load_json_export(json_path):
    path = Path(json_path)
    if not json_path or not path.is_file():
        raise ValueError('JSON 文件不存在：请填写运行本程序的电脑上的文件路径')
    try:
        with path.open('r', encoding='utf-8-sig') as stream:
            data = json.load(stream)
    except (OSError, ValueError) as exc:
        raise ValueError(f'JSON 解析失败，请检查文件权限和内容：{exc}') from exc
    if not isinstance(data, dict) or not isinstance(data.get('messages'), list) or any(
        not isinstance(message, dict) for message in data['messages']
    ):
        raise ValueError('JSON 格式无效：需要包含 messages 数组的单个聊天导出文件')
    return data
