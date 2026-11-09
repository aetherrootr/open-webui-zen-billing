"""
title: Zen 费用报表
author: aetherrootr
version: 1.0.0
license: MIT
description: 无需调用模型，查看本月或指定月份的 Zen 用户费用汇总。
"""

import re


class Pipe:
    async def pipe(self, body, __user__=None, __request__=None):
        if not __user__ or not __user__.get('id'):
            return '请先登录。'
        from open_webui.utils.plugin import get_function_module_from_cache

        tracker, _, _ = await get_function_module_from_cache(__request__, 'zen_cost_tracker')
        messages = body.get('messages', [])
        last = next((m.get('content', '') for m in reversed(messages) if m.get('role') == 'user'), '')
        match = re.search(r'\b(20\d{2}-(?:0[1-9]|1[0-2]))\b', last if isinstance(last, str) else '')
        return await tracker.report(__user__, match.group(1) if match else None)
