"""
title: Zen 费用报表
author: aetherrootr
version: 1.0.0
license: MIT
description: 无需调用模型，查看本月或指定月份的 Zen 用户费用汇总。
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo


HELP = """### Zen 费用报表 · 查询帮助

先选择 **Zen 费用报表** 模型，再点击预制提示词或发送以下内容：

| 输入 | 功能 |
|---|---|
| `查看本月费用` / `本月` | 查询本月费用 |
| `查看上月费用` / `上月` | 查询上月费用 |
| `2026-11` / `查看 2026-11 费用` | 查询指定月份，格式为 YYYY-MM |
| `/prompt 本月` / `/prompt 上月` / `/prompt 2026-11` | 使用查询命令 |
| `/prompt` / `/zen-billing` / `help` / `帮助` | 显示此帮助 |

在输入框输入 `/` 可选择安装器注册的预制提示词，例如 `/zen-billing-month`。
预制提示词只填入查询内容，不会自动切换模型。
管理员可查看所有用户；普通用户只能查看自己的费用。查询本身不调用付费模型。
"""


def parse_query(content):
    if not isinstance(content, str):
        return 'help', None
    query = content.strip().lower()
    for command in ('/prompt', '/zen-billing'):
        if query == command:
            return 'help', None
        if query.startswith(command + ' '):
            query = query[len(command) :].strip()
            break
    if query in ('本月', '本月费用', '查看本月费用', '查询本月费用'):
        return 'current', None
    if query in ('上月', '上月费用', '查看上月费用', '查询上月费用'):
        return 'previous', None
    match = re.fullmatch(r'(?:(?:查看|查询)\s*)?(20\d{2}-(?:0[1-9]|1[0-2]))(?:\s*费用)?', query)
    if match:
        return 'month', match.group(1)
    return 'help', None


def last_text(messages):
    content = next((m.get('content', '') for m in reversed(messages) if m.get('role') == 'user'), '')
    if isinstance(content, list):
        return '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
    return content


class Pipe:
    async def pipe(self, body, __user__=None, __request__=None):
        if not __user__ or not __user__.get('id'):
            return '请先登录。'
        kind, month = parse_query(last_text(body.get('messages', [])))
        if kind == 'help':
            return HELP
        from open_webui.utils.plugin import get_function_module_from_cache

        tracker, _, _ = await get_function_module_from_cache(__request__, 'zen_cost_tracker')
        if kind == 'previous':
            now = datetime.now(ZoneInfo(tracker.valves.timezone))
            year, mon = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
            month = f'{year}-{mon:02d}'
        return await tracker.report(__user__, month)
