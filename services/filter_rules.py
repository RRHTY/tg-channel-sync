"""Shared Python-regex semantics and a bounded, disposable preview."""
import multiprocessing
import re
import threading

_preview_lock = threading.Lock()


def compile_filter_regex(pattern, is_case_sensitive):
    return re.compile(pattern, 0 if is_case_sensitive else re.IGNORECASE)


def apply_filter_rule(rule_type, regex, replacement, text, file_name):
    if rule_type in {'drop', 'skip_media'}:
        return bool(regex.search(text) or (file_name and regex.search(file_name))), text
    if rule_type in {'replace', 'replace_text'} and text:
        return False, regex.sub(replacement or '', text)
    return False, text


def validate_filter_rule(rule_type, pattern, replacement='', is_case_sensitive=0):
    if rule_type not in {'replace', 'replace_text', 'drop', 'skip_media'}:
        raise ValueError('无效的过滤规则类型')
    if not pattern or len(pattern) > 1000 or len(replacement) > 1000:
        raise ValueError('正则不可为空；正则和替换内容各限 1000 字符')
    if is_case_sensitive not in (0, 1):
        raise ValueError('大小写选项无效')
    try:
        regex = compile_filter_regex(pattern, is_case_sensitive)
        if rule_type in {'replace', 'replace_text'}:
            # Validate references against identical group metadata, never execute
            # the user's pattern: even matching an empty string can be expensive.
            names = {index: name for name, index in regex.groupindex.items()}
            safe_pattern = ''.join(f'(?P<{names[index]}>)' if index in names else '()' for index in range(1, regex.groups + 1))
            re.compile(safe_pattern).sub(replacement, '', count=1)
    except (re.error, IndexError, RecursionError, OverflowError) as exc:
        raise ValueError(f'正则或替换内容无效：{exc}') from exc
    return regex


def _preview_child(connection, args):
    try:
        rule_type, pattern, replacement, sensitive, text, file_name = args
        regex = validate_filter_rule(rule_type, pattern, replacement, sensitive)
        dropped, result = apply_filter_rule(rule_type, regex, replacement, text, file_name)
        connection.send({'dropped': dropped, 'matched': bool(regex.search(text) or (rule_type in {'drop', 'skip_media'} and regex.search(file_name))), 'text': result[:8000]})
    except Exception as exc:
        connection.send({'error': str(exc)})
    finally:
        connection.close()


def preview_filter_rule(rule_type, pattern, replacement, sensitive, text, file_name, *, timeout=5):
    validate_filter_rule(rule_type, pattern, replacement, sensitive)
    if len(text) > 4000 or len(file_name) > 255:
        raise ValueError('预览文本限 4000 字符，文件名限 255 字符')
    if not _preview_lock.acquire(blocking=False):
        raise ValueError('另一个预览正在运行，请稍后重试')
    process = None
    receiver = sender = None
    try:
        context = multiprocessing.get_context('spawn')
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_preview_child, args=(sender, (rule_type, pattern, replacement, sensitive, text, file_name)), daemon=True)
        process.start()
        sender.close()
        if not receiver.poll(timeout):
            raise ValueError('正则预览超时，请简化表达式后重试')
        try:
            result = receiver.recv()
        except EOFError as exc:
            raise ValueError('预览进程未能完成，请重试') from exc
        if 'error' in result:
            raise ValueError(result['error'])
        return result
    finally:
        if process is not None and process.pid is not None:
            process.join(.1)
            if process.is_alive():
                process.terminate()
                process.join(1)
            process.close()
        if receiver is not None:
            receiver.close()
        if sender is not None:
            sender.close()
        _preview_lock.release()
