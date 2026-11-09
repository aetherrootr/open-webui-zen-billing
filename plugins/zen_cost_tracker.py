"""
title: Zen 用户费用统计
author: aetherrootr
version: 1.0.0
license: MIT
description: 按用户记录共享 Zen 连接的实际费用或目录估算费用；不保存提示词。
"""

import asyncio
import copy
import json
import logging
import time
import uuid
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from pydantic import BaseModel, Field
from sqlalchemy import text

log = logging.getLogger(__name__)
TABLE = 'zen_user_cost_entry'


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except Exception:
        return None


def tokens(usage):
    def count(*keys, default=0):
        for key in keys:
            value = number(usage.get(key))
            if value is not None and value == int(value):
                return int(value)
        return default

    canonical = (usage.get('opencode') or {}).get('tokens')
    if isinstance(canonical, dict) and all(k in canonical for k in ('input', 'output')):
        counts = {}
        for key in ('input', 'output', 'cache_read', 'cache_write_5m', 'cache_write_1h'):
            value = number(canonical.get(key, 0))
            if value is None or value != int(value):
                raise ValueError('Zen 原始 Token 数据不一致')
            counts[key] = int(value)
        write = counts['cache_write_5m'] + counts['cache_write_1h']
        return {
            'input': counts['input'],
            'output': counts['output'],
            'cache_read': counts['cache_read'],
            'cache_write': write,
            'cache_write_1h': counts['cache_write_1h'],
            'context': counts['input'] + counts['cache_read'] + write,
        }

    inp = count('input_tokens', 'prompt_tokens', default=None)
    out = count('output_tokens', 'completion_tokens', default=None)
    if inp is None or out is None:
        raise ValueError('缺少上游输入或输出 Token 数量')
    details = usage.get('input_tokens_details') or usage.get('prompt_tokens_details') or {}
    read = count('cache_read_input_tokens', 'cached_tokens', default=details.get('cached_tokens', 0))
    creation = usage.get('cache_creation') or {}
    write = count(
        'cache_creation_input_tokens', 'cache_write_input_tokens', default=details.get('cache_write_tokens', 0)
    )
    write_1h = creation.get('ephemeral_1h_input_tokens', 0)
    if not write:
        write = creation.get('ephemeral_5m_input_tokens', 0) + write_1h
    # Anthropic input excludes caches; OpenAI prompt/input includes them.
    native_anthropic = 'prompt_tokens' not in usage and any(
        k in usage for k in ('cache_read_input_tokens', 'cache_creation_input_tokens')
    )
    fresh = inp if native_anthropic else inp - read - write
    if min(fresh, read, write, write_1h) < 0:
        raise ValueError('上游缓存 Token 数据不一致')
    return {
        'input': fresh,
        'output': out,
        'cache_read': read,
        'cache_write': write,
        'cache_write_1h': write_1h,
        'context': fresh + read + write,
    }


def select_rate(rate, context):
    result = copy.deepcopy(rate)
    for override in rate.get('overrides', []):
        condition = override.get('when', {})
        threshold = condition.get('context', {}).get('gt')
        if set(condition) != {'context'} or set(condition['context']) != {'gt'}:
            raise ValueError('目录含暂不支持的价格条件')
        if context > threshold:
            result.update({k: v for k, v in override.items() if k != 'when'})
    return result


def calculate(usage, model):
    reported = number(usage.get('cost'))
    if reported is not None:
        source = 'estimated' if (usage.get('opencode') or {}).get('estimated') else 'reported'
        return reported, source, {'provider_cost': str(reported)}
    t = tokens(usage)
    if not model:
        raise ValueError('Zen 目录中没有此模型的精确价格')
    pricing = model.get('cost', {}).get('text', {})
    inp = select_rate(pricing.get('input', {}), t['context'])
    out = select_rate(pricing.get('output', {}), t['context'])
    cache = inp.get('cache', {})
    if t['cache_write_1h']:
        raise ValueError('目录未明确提供一小时缓存写入单价')
    rates = {
        'input': inp.get('price'),
        'output': out.get('price'),
        'cache_read': cache.get('read'),
        'cache_write': cache.get('write'),
    }
    total = Decimal(0)
    for key, raw in rates.items():
        rate = number(raw)
        if t[key] and rate is None:
            raise ValueError('缺少 ' + key + ' 单价')
        total += Decimal(t[key]) * (rate or Decimal(0)) / Decimal(1000000)
    if number(rates['input']) is None or number(rates['output']) is None:
        raise ValueError('缺少输入或输出价格')
    return total, 'estimated', {'tokens': t, 'usd_per_million': rates}


async def database():
    from open_webui.internal.db import get_async_db_context

    return get_async_db_context()


async def ensure_table(db):
    await db.execute(
        text("""CREATE TABLE IF NOT EXISTS zen_user_cost_entry (
        record_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, user_name TEXT NOT NULL,
        created_at BIGINT NOT NULL, model_id TEXT NOT NULL, chat_id TEXT,
        message_id TEXT, amount TEXT, source TEXT NOT NULL, detail TEXT NOT NULL
    )""")
    )
    await db.execute(
        text('CREATE INDEX IF NOT EXISTS zen_user_cost_time_user ON zen_user_cost_entry (created_at, user_id)')
    )
    await db.execute(
        text("""CREATE TABLE IF NOT EXISTS zen_user_cost_call (
        attempt_id TEXT PRIMARY KEY, record_id TEXT NOT NULL, provider_id TEXT,
        amount TEXT, source TEXT NOT NULL, detail TEXT NOT NULL
    )""")
    )
    await db.execute(text('CREATE INDEX IF NOT EXISTS zen_cost_call_record ON zen_user_cost_call (record_id)'))


def entry_values(context, amount=None, source='unpriced', detail=None):
    return {
        k: context.get(k)
        for k in ('record_id', 'user_id', 'user_name', 'created_at', 'model_id', 'chat_id', 'message_id')
    } | {
        'amount': str(amount) if amount is not None else None,
        'source': source,
        'detail': json.dumps(detail or {'state': 'pending'}, ensure_ascii=False, default=str),
    }


async def lock_entry(db, values):
    await ensure_table(db)
    await db.execute(
        text("""INSERT INTO zen_user_cost_entry
        (record_id,user_id,user_name,created_at,model_id,chat_id,message_id,amount,source,detail)
        VALUES (:record_id,:user_id,:user_name,:created_at,:model_id,:chat_id,:message_id,:amount,:source,:detail)
        ON CONFLICT (record_id) DO NOTHING"""),
        values,
    )
    # Serialize checkpoints and finalization for one generation on both PostgreSQL and SQLite.
    result = await db.execute(
        text("""UPDATE zen_user_cost_entry SET detail=detail
        WHERE record_id=:record_id AND user_id=:user_id"""),
        values,
    )
    if not result.rowcount:
        raise ValueError('计费记录身份不匹配')
    return (
        (await db.execute(text('SELECT * FROM zen_user_cost_entry WHERE record_id=:record_id'), values))
        .mappings()
        .one()
    )


class Filter:
    class Valves(BaseModel):
        priority: int = 90
        refresh_seconds: int = Field(default=3600, ge=60)
        timezone: str = 'Asia/Shanghai'
        show_status: bool = True

    def __init__(self):
        self.valves = self.Valves()
        self._catalogs = {}
        self._lock = asyncio.Lock()
        self._writes = set()

    async def durable(self, operation):
        task = asyncio.create_task(operation)
        self._writes.add(task)

        def finished(done):
            self._writes.discard(done)
            if not done.cancelled() and done.exception():
                log.error('Zen cost checkpoint failed: %s', type(done.exception()).__name__)

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def begin_attempt(self, context, attempt):
        async with await database() as db:
            await lock_entry(db, entry_values(context))
            await db.execute(
                text("""INSERT INTO zen_user_cost_call
                (attempt_id,record_id,provider_id,amount,source,detail)
                VALUES (:attempt,:record,NULL,NULL,'unpriced','{}')
                ON CONFLICT (attempt_id) DO NOTHING"""),
                {'attempt': attempt, 'record': context['record_id']},
            )
            await db.commit()

    async def checkpoint(self, context, attempt, provider_id, usage):
        cached = self._catalogs.get(context['url'], {})
        model = cached.get('models', {}).get(context['model_id'])
        detail = {'usage': usage}
        amount, source = None, 'unpriced'
        try:
            amount, source, calculation = calculate(usage, model)
            detail['calculation'] = calculation
        except (ValueError, TypeError, KeyError) as exc:
            detail['error'] = str(exc)
        async with await database() as db:
            current = await lock_entry(db, entry_values(context))
            await db.execute(
                text("""INSERT INTO zen_user_cost_call
                (attempt_id,record_id,provider_id,amount,source,detail)
                VALUES (:attempt,:record,:provider,:amount,:source,:detail)
                ON CONFLICT (attempt_id) DO UPDATE SET
                provider_id=excluded.provider_id, amount=excluded.amount, source=excluded.source, detail=excluded.detail"""),
                {
                    'attempt': attempt,
                    'record': context['record_id'],
                    'provider': provider_id,
                    'amount': str(amount) if amount is not None else None,
                    'source': source,
                    'detail': json.dumps(detail, ensure_ascii=False, default=str),
                },
            )
            root_detail = json.loads(current['detail'])
            if root_detail.get('state') != 'complete':
                calls = (
                    (
                        await db.execute(
                            text('SELECT amount,source FROM zen_user_cost_call WHERE record_id=:record'),
                            {'record': context['record_id']},
                        )
                    )
                    .mappings()
                    .all()
                )
                known = [c for c in calls if c['amount'] is not None]
                total = sum((Decimal(c['amount']) for c in known), Decimal(0)) if known else None
                source = (
                    ('reported' if all(c['source'] == 'reported' for c in known) else 'estimated')
                    if known
                    else 'unpriced'
                )
                root_detail.update(
                    {
                        'state': 'observed',
                        'observed_calls': len(known),
                        'pending_calls': len(calls) - len(known),
                        'currency': 'USD',
                    }
                )
                await db.execute(
                    text("""UPDATE zen_user_cost_entry SET amount=:amount,source=:source,detail=:detail
                    WHERE record_id=:record"""),
                    {
                        'record': context['record_id'],
                        'amount': str(total) if total is not None else None,
                        'source': source,
                        'detail': json.dumps(root_detail, ensure_ascii=False),
                    },
                )
            await db.commit()

    async def finalize(self, values):
        async with await database() as db:
            current = await lock_entry(db, values)
            if json.loads(current['detail']).get('state') != 'complete':
                if values['amount'] is None and current['amount'] is not None:
                    values = {**values, 'amount': current['amount'], 'source': current['source']}
                await db.execute(
                    text("""UPDATE zen_user_cost_entry SET amount=:amount,source=:source,detail=:detail
                    WHERE record_id=:record_id AND user_id=:user_id"""),
                    values,
                )
            await db.commit()

    async def connection(self, model, request):
        if request is None or not isinstance(model, dict):
            return None
        base = (model.get('info') or {}).get('base_model_id') or model.get('id')
        backend_model = request.app.state.OPENAI_MODELS.get(base, model)
        idx = backend_model.get('urlIdx')
        if idx is None:
            return None
        from open_webui.routers.openai import get_openai_connection

        url, key, config = await get_openai_connection(idx)
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'opencode.ai':
            return None
        if parsed.path.rstrip('/') not in ('/zen/v1', '/inference/openai/v1', '/inference/anthropic/v1'):
            return None
        prefix = config.get('prefix_id')
        if prefix and base.startswith(prefix + '.'):
            base = base[len(prefix) + 1 :]
        return url.rstrip('/'), key, base

    async def catalog(self, url, key):
        async with self._lock:
            cached = self._catalogs.get(url)
            if cached and time.time() - cached['at'] < self.valves.refresh_seconds:
                return cached
            headers = {'Authorization': 'Bearer ' + key, 'User-Agent': 'OpenWebUI-Zen-Cost-Tracker/1.0'}
            endpoint = (
                'https://opencode.ai/zen/v1/models' if '/zen/' in url else 'https://opencode.ai/inference/v1/models'
            )
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=True) as session:
                    async with session.get(endpoint, headers=headers, allow_redirects=False) as response:
                        response.raise_for_status()
                        data = await response.json()
                models = {m['id']: m for m in data['data'] if isinstance(m.get('cost'), dict)}
                if not models:
                    raise ValueError('Zen 未返回带价格的模型目录')
                cached = {'at': int(time.time()), 'endpoint': endpoint, 'models': models}
                self._catalogs[url] = cached
                return cached
            except Exception:
                if cached:
                    return cached
                raise ValueError('无法获取 Zen 官方价格目录') from None

    async def inlet(self, body, __metadata__=None, __model__=None, __request__=None, __user__=None):
        connection = await self.connection(__model__, __request__)
        if connection:
            if __metadata__ is None or not (__user__ or {}).get('id'):
                raise RuntimeError('Zen 计费请求缺少用户身份或请求上下文，已阻止调用')
            record = str(uuid.uuid4())
            __metadata__['zen_cost_record'] = record
            __metadata__['zen_cost_calls'] = {}
            __metadata__['zen_cost_attempts'] = {}
            __metadata__['zen_cost_active_attempt'] = str(uuid.uuid4())
            __metadata__.pop('zen_cost_active_provider', None)
            __metadata__['zen_cost_seen_event'] = False
            __metadata__['zen_cost_request_seen'] = False
            context = {
                'record_id': record,
                'user_id': __user__['id'],
                'user_name': __user__.get('name', __user__['id']),
                'created_at': int(time.time()),
                'model_id': connection[2],
                'url': connection[0],
                'chat_id': __metadata__.get('chat_id') or body.get('chat_id'),
                'message_id': __metadata__.get('message_id') or body.get('id'),
            }
            __metadata__['zen_cost_context'] = context
            try:
                await self.durable(self.begin_attempt(context, __metadata__['zen_cost_active_attempt']))
            except Exception:
                raise RuntimeError('无法登记 Zen 计费请求，已阻止调用，请稍后重试') from None
        return body

    async def request(self, body, __model__=None, __request__=None, __metadata__=None, __user__=None):
        if await self.connection(__model__, __request__):
            if __metadata__ is None or 'zen_cost_context' not in __metadata__:
                await self.inlet(body, __metadata__, __model__, __request__, __user__)
            if __metadata__['zen_cost_request_seen'] or __metadata__['zen_cost_seen_event']:
                __metadata__['zen_cost_active_attempt'] = str(uuid.uuid4())
                __metadata__.pop('zen_cost_active_provider', None)
                __metadata__['zen_cost_seen_event'] = False
                try:
                    await self.durable(
                        self.begin_attempt(__metadata__['zen_cost_context'], __metadata__['zen_cost_active_attempt'])
                    )
                except Exception:
                    raise RuntimeError('无法登记 Zen 计费请求，已阻止调用，请稍后重试') from None
            __metadata__['zen_cost_request_seen'] = True
            if body.get('stream'):
                body['stream_options'] = {**(body.get('stream_options') or {}), 'include_usage': True}
        return body

    async def stream(self, event, __metadata__=None):
        if __metadata__ is None or 'zen_cost_calls' not in __metadata__:
            return event
        __metadata__['zen_cost_seen_event'] = True
        payload = event.get('response') if isinstance(event.get('response'), dict) else event
        if event.get('type') == 'message_start' and isinstance(event.get('message'), dict):
            payload = event['message']
        provider = (
            payload.get('id')
            or __metadata__.get('zen_cost_active_provider')
            or __metadata__.get('zen_cost_active_attempt')
        )
        if payload.get('id'):
            __metadata__['zen_cost_active_provider'] = payload['id']
        if isinstance(payload, dict) and provider and isinstance(payload.get('usage'), dict) and payload['usage']:
            usage = {**__metadata__['zen_cost_calls'].get(provider, {}), **copy.deepcopy(payload['usage'])}
            __metadata__['zen_cost_calls'][provider] = usage
            context = __metadata__.get('zen_cost_context')
            if context:
                attempt = __metadata__['zen_cost_attempts'].setdefault(
                    provider, __metadata__['zen_cost_active_attempt']
                )
                await self.durable(self.checkpoint(context, attempt, provider, usage))
        return event

    async def outlet(
        self, body, __user__=None, __metadata__=None, __model__=None, __request__=None, __event_emitter__=None
    ):
        user = __user__ or {}
        metadata = __metadata__ or {}
        connection = await self.connection(__model__, __request__)
        if not connection or not user.get('id'):
            return body
        message = next((m for m in reversed(body.get('messages', [])) if m.get('role') == 'assistant'), None)
        if not message:
            return body
        usage = message.get('usage') or {}
        url, key, model_id = connection
        detail = {'usage': usage, 'currency': 'USD'}
        amount, source = None, 'unpriced'
        try:
            # A provider-reported amount remains usable during a catalog outage.
            try:
                catalog = await self.catalog(url, key)
            except ValueError:
                if number(usage.get('cost')) is None:
                    raise
                catalog = {'models': {}, 'at': None, 'endpoint': None}
            model = catalog['models'].get(model_id)
            if not usage:
                raise ValueError('上游未返回用量；请开启模型 Usage')
            if usage.get('partial') or usage.get('usage_partial') or usage.get('incomplete'):
                raise ValueError('上游只返回部分调用的用量')
            calls = list(metadata.get('zen_cost_calls', {}).values())
            if number(usage.get('cost')) is not None:
                # The server's merged billed amount is authoritative across tool rounds.
                calls = [usage]
            # Prefer per-call data for correct cache and context-tier calculation.
            if calls and number(usage.get('cost')) is None:
                observed = [tokens(call) for call in calls]
                merged = tokens(usage)
                if sum(t['context'] + t['output'] for t in observed) != merged['context'] + merged['output']:
                    calls = []
            if not calls:
                calls = [usage]
            calculated = [calculate(call, model) for call in calls]
            amount = sum((c[0] for c in calculated), Decimal(0))
            source = 'reported' if all(c[1] == 'reported' for c in calculated) else 'estimated'
            detail.update(
                {
                    'catalog_at': catalog['at'],
                    'catalog_endpoint': catalog['endpoint'],
                    'calculations': [c[2] for c in calculated],
                }
            )
            if len(calls) == 1 and usage.get('input_tokens') != usage.get('prompt_tokens') and 'prompt_tokens' in usage:
                detail['note'] = '按合并用量估算，工具多轮的阶梯价格可能有差异'
        except (ValueError, KeyError, TypeError) as exc:
            detail['error'] = str(exc)
        detail['state'] = 'complete' if amount is not None else 'unresolved'
        record_id = metadata.get('zen_cost_record') or ':'.join(
            [user['id'], str(body.get('chat_id')), str(body.get('id') or message.get('id'))]
        )
        values = dict(
            record_id=record_id,
            user_id=user['id'],
            user_name=user.get('name', user['id']),
            created_at=(metadata.get('zen_cost_context') or {}).get('created_at', int(time.time())),
            model_id=model_id,
            chat_id=body.get('chat_id'),
            message_id=body.get('id') or message.get('id'),
            amount=str(amount) if amount is not None else None,
            source=source,
            detail=json.dumps(detail, ensure_ascii=False, default=str),
        )
        try:
            await self.durable(self.finalize(values))
            if __event_emitter__ and self.valves.show_status:
                label = {'reported': '上游费用', 'estimated': '估算费用', 'unpriced': '费用待核对'}[source]
                value = f'${amount:.8f}' if amount is not None else detail.get('error', '')
                await __event_emitter__(
                    {'type': 'status', 'data': {'description': f'Zen {label}：{value}', 'done': True}}
                )
        except Exception:
            log.exception('Zen cost ledger write failed')
        return body

    async def report(self, user, month=None):
        zone = ZoneInfo(self.valves.timezone)
        now = datetime.now(zone)
        year, mon = map(int, month.split('-')) if month else (now.year, now.month)
        start = datetime(year, mon, 1, tzinfo=zone)
        end = datetime(year + (mon == 12), 1 if mon == 12 else mon + 1, 1, tzinfo=zone)
        query = 'SELECT user_id,user_name,model_id,amount,source,detail FROM zen_user_cost_entry WHERE created_at >= :start AND created_at < :end'
        params = {'start': int(start.timestamp()), 'end': int(end.timestamp())}
        if user.get('role') != 'admin':
            query += ' AND user_id = :user'
            params['user'] = user['id']
        async with await database() as db:
            await ensure_table(db)
            rows = (await db.execute(text(query), params)).mappings().all()
            await db.commit()
        groups = {}
        for row in rows:
            key = (row['user_id'], row['user_name'], row['model_id'])
            group = groups.setdefault(key, {'reported': Decimal(0), 'estimated': Decimal(0), 'unpriced': 0, 'count': 0})
            group['count'] += 1
            if row['amount'] is None or json.loads(row['detail']).get('state', 'complete') != 'complete':
                group['unpriced'] += 1
            if row['amount'] is not None:
                group[row['source']] += Decimal(row['amount'])

        def clean(value):
            return str(value).replace('|', '\\|').replace('\n', ' ')

        lines = [
            f'### Zen 用户费用 · {year}-{mon:02d}',
            f'币种 USD，时区 {self.valves.timezone}。管理员可见所有用户，普通用户仅可见本人。',
            '',
            '| 用户 | 模型 | 请求数 | 上游费用 | 估算费用 | 待核对 |',
            '|---|---|---:|---:|---:|---:|',
        ]
        for (_, name, model_id), g in sorted(groups.items()):
            lines.append(
                f'| {clean(name)} | {clean(model_id)} | {g["count"]} | ${g["reported"]:.8f} | ${g["estimated"]:.8f} | {g["unpriced"]} |'
            )
        reported = sum((g['reported'] for g in groups.values()), Decimal(0))
        estimated = sum((g['estimated'] for g in groups.values()), Decimal(0))
        users = {}
        for (uid, name, _), group in groups.items():
            totals = users.setdefault((uid, name), {'reported': Decimal(0), 'estimated': Decimal(0), 'unpriced': 0})
            for field in totals:
                totals[field] += group[field]
        lines += ['', '| 用户汇总 | 上游费用 | 估算费用 | 待核对 |', '|---|---:|---:|---:|']
        for (_, name), total in sorted(users.items()):
            lines.append(
                f'| {clean(name)} | ${total["reported"]:.8f} | ${total["estimated"]:.8f} | {total["unpriced"]} |'
            )
        lines += [
            '',
            f'**合计：上游费用 ${reported:.8f}；估算费用 ${estimated:.8f}；待核对 {sum(g["unpriced"] for g in groups.values())} 条。**',
            '登记请求不代表已扣费。待核对包含进行中或中断后缺少最终用量的请求；已观测金额会计入，仍可能有未知费用。估算费用不等同于 Zen 扣款账单。',
        ]
        if not rows:
            lines.append('本月暂无费用记录。')
        return '\n'.join(lines)
