"""Run with the application's Python dependencies, never the production ledger."""

import copy
import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import text

import zen_cost_tracker as tracker

HAIKU = {'cost': {'text': {'input': {'price': 1, 'cache': {'read': 0.1, 'write': 1.25}}, 'output': {'price': 5}}}}
TIERED = {
    'cost': {
        'text': {
            'input': {
                'price': 0.1,
                'cache': {'read': 0.01},
                'overrides': [{'when': {'context': {'gt': 100000}}, 'price': 0.5, 'cache': {'read': 0.05}}],
            },
            'output': {'price': 0.5, 'overrides': [{'when': {'context': {'gt': 100000}}, 'price': 2.5}]},
        }
    }
}


class PriceTests(unittest.TestCase):
    def test_ordinary_price_per_million(self):
        self.assertEqual(
            tracker.calculate({'prompt_tokens': 1000000, 'completion_tokens': 1000000}, HAIKU)[0], Decimal('6')
        )

    def test_openai_cache_is_part_of_input(self):
        usage = {
            'prompt_tokens': 1000000,
            'completion_tokens': 100000,
            'prompt_tokens_details': {'cached_tokens': 800000},
        }
        self.assertEqual(tracker.calculate(usage, HAIKU)[0], Decimal('0.78'))

    def test_anthropic_cache_is_additional_to_input(self):
        usage = {'input_tokens': 200000, 'cache_read_input_tokens': 800000, 'output_tokens': 100000}
        self.assertEqual(tracker.calculate(usage, HAIKU)[0], Decimal('0.78'))

    def test_cache_creation(self):
        usage = {'input_tokens': 100, 'cache_creation_input_tokens': 1000, 'output_tokens': 100}
        self.assertEqual(tracker.calculate(usage, HAIKU)[0], Decimal('0.00185'))

    def test_openai_cache_write_details(self):
        usage = {'prompt_tokens': 1100, 'completion_tokens': 100, 'prompt_tokens_details': {'cache_write_tokens': 1000}}
        self.assertEqual(tracker.calculate(usage, HAIKU)[0], Decimal('0.00185'))

    def test_zen_canonical_tokens_exclude_cache_from_fresh_input(self):
        usage = {
            'prompt_tokens': 1000000,
            'completion_tokens': 100000,
            'opencode': {'tokens': {'input': 200000, 'output': 100000, 'cache_read': 800000}},
        }
        self.assertEqual(tracker.calculate(usage, HAIKU)[0], Decimal('0.78'))

    def test_tier_boundary(self):
        for count, expected in [(100000, '0.0105'), (100001, '0.0525005')]:
            self.assertEqual(
                tracker.calculate({'prompt_tokens': count, 'completion_tokens': 1000}, TIERED)[0], Decimal(expected)
            )

    def test_reported_cost_including_zero(self):
        for value in [0, '0.123456789']:
            amount, source, _ = tracker.calculate({'cost': value}, None)
            self.assertEqual(amount, Decimal(str(value)))
            self.assertEqual(source, 'reported')

    def test_unknown_model_is_not_free(self):
        with self.assertRaises(ValueError):
            tracker.calculate({'prompt_tokens': 10, 'completion_tokens': 10}, None)

    def test_provider_estimate_is_not_labelled_billed(self):
        result = tracker.calculate({'cost': 0.2, 'opencode': {'estimated': True}}, None)
        self.assertEqual(result[1], 'estimated')

    def test_missing_usage_is_not_free(self):
        with self.assertRaises(ValueError):
            tracker.calculate({}, HAIKU)

    def test_invalid_numeric_cost(self):
        for value in [-1, 'NaN', 'Infinity', True]:
            self.assertIsNone(tracker.number(value))

    def test_missing_cache_price_is_not_full_price(self):
        with self.assertRaises(ValueError):
            tracker.calculate(
                {'prompt_tokens': 10, 'completion_tokens': 10, 'cached_tokens': 5},
                {'cost': {'text': {'input': {'price': 1}, 'output': {'price': 5}}}},
            )

    def test_reasoning_is_not_double_counted(self):
        usage = {
            'prompt_tokens': 1000,
            'completion_tokens': 1000,
            'completion_tokens_details': {'reasoning_tokens': 500},
        }
        self.assertEqual(tracker.calculate(usage, HAIKU)[0], Decimal('0.006'))

    def test_real_zen_catalog_has_exact_known_prices(self):
        fixture = Path(__file__).parent / 'fixtures/zen-catalog.json'
        models = {m['id']: m for m in json.loads(fixture.read_text())['data']}
        self.assertEqual(
            tracker.calculate({'prompt_tokens': 1000000, 'completion_tokens': 1000000}, models['claude-haiku-4-5'])[0],
            Decimal(6),
        )
        self.assertEqual(
            tracker.calculate({'prompt_tokens': 1000, 'completion_tokens': 1000}, models['longcat-2.5-free'])[0],
            Decimal(0),
        )
        self.assertTrue(all(m.get('cost') for m in models.values()))


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        factory = async_sessionmaker(self.engine)

        @asynccontextmanager
        async def context():
            async with factory() as db:
                yield db

        async def database():
            return context()

        self.db_patch = patch.object(tracker, 'database', database)
        self.db_patch.start()
        self.filter = tracker.Filter()
        self.filter.connection = AsyncMock(
            return_value=('https://opencode.ai/zen/v1', 'not-a-real-key', 'claude-haiku-4-5')
        )
        self.filter.catalog = AsyncMock(
            return_value={'models': {'claude-haiku-4-5': HAIKU}, 'at': 123, 'endpoint': 'fixture'}
        )
        self.user_a = {'id': 'a', 'name': 'Alice', 'role': 'user'}
        self.user_b = {'id': 'b', 'name': 'Bob', 'role': 'user'}
        self.body = {
            'chat_id': 'chat',
            'id': 'reply',
            'messages': [
                {'id': 'reply', 'role': 'assistant', 'usage': {'prompt_tokens': 1000000, 'completion_tokens': 1000000}}
            ],
        }

    async def asyncTearDown(self):
        self.db_patch.stop()
        await self.engine.dispose()

    async def record(self, user, metadata=None, body=None):
        body = copy.deepcopy(body or self.body)
        await self.filter.outlet(body, __user__=user, __metadata__=metadata)

    async def test_user_separation_and_admin_totals(self):
        await self.record(self.user_a)
        await self.record(self.user_b)
        a = await self.filter.report(self.user_a)
        self.assertIn('Alice', a)
        self.assertNotIn('Bob', a)
        self.assertIn('$6.00000000', a)
        admin = await self.filter.report({'id': 'admin', 'role': 'admin'})
        self.assertIn('Alice', admin)
        self.assertIn('Bob', admin)
        self.assertIn('$12.00000000', admin)

    async def test_duplicate_outlet_is_idempotent(self):
        await self.record(self.user_a)
        await self.record(self.user_a)
        report = await self.filter.report(self.user_a)
        self.assertIn('$6.00000000', report)
        self.assertNotIn('$12.00000000', report)

    async def test_new_generation_with_same_message_is_counted(self):
        await self.record(self.user_a, {'zen_cost_record': 'generation-1'})
        await self.record(self.user_a, {'zen_cost_record': 'generation-2'})
        self.assertIn('$12.00000000', await self.filter.report(self.user_a))

    async def test_incomplete_usage_is_flagged(self):
        body = copy.deepcopy(self.body)
        body['messages'][0]['usage']['incomplete'] = True
        await self.record(self.user_a, body=body)
        self.assertIn('待核对 1 条', await self.filter.report(self.user_a))

    async def test_catalog_outage_still_records_reported_cost(self):
        self.filter.catalog.side_effect = ValueError('offline')
        body = copy.deepcopy(self.body)
        body['messages'][0]['usage']['cost'] = 0.2
        await self.record(self.user_a, body=body)
        self.assertIn('上游费用 $0.20000000', await self.filter.report(self.user_a))

    async def test_non_zen_request_is_ignored(self):
        self.filter.connection.return_value = None
        await self.record(self.user_a)
        self.assertIn('本月暂无', await self.filter.report(self.user_a))

    async def test_merged_billed_cost_wins_over_per_call_estimates(self):
        body = copy.deepcopy(self.body)
        body['messages'][0]['usage']['cost'] = 0.1
        calls = {'call': {'prompt_tokens': 1000000, 'completion_tokens': 1000000}}
        await self.record(self.user_a, {'zen_cost_calls': calls}, body)
        report = await self.filter.report(self.user_a)
        self.assertIn('上游费用 $0.10000000', report)
        self.assertIn('估算费用 $0.00000000', report)

    async def test_stream_repeated_usage_replaces_instead_of_summing(self):
        metadata = {'zen_cost_calls': {}}
        event = {'id': 'call-1', 'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}
        await self.filter.stream(event, metadata)
        await self.filter.stream(event, metadata)
        self.assertEqual(len(metadata['zen_cost_calls']), 1)

    async def test_per_call_tiers_not_merged_tier(self):
        self.filter.catalog.return_value['models']['claude-haiku-4-5'] = TIERED
        usage = {'input_tokens': 160000, 'output_tokens': 2000, 'prompt_tokens': 80000, 'completion_tokens': 1000}
        body = copy.deepcopy(self.body)
        body['messages'][0]['usage'] = usage
        calls = {str(i): {'prompt_tokens': 80000, 'completion_tokens': 1000} for i in range(2)}
        await self.record(self.user_a, {'zen_cost_calls': calls}, body)
        self.assertIn('$0.01700000', await self.filter.report(self.user_a))

    async def start_request(self):
        metadata = {'chat_id': 'deleted-later', 'message_id': 'reply'}
        await self.filter.inlet({}, __metadata__=metadata, __user__=self.user_a)
        return metadata

    async def stream_cost(self, metadata, provider='call-1', cost=0.1):
        return await self.filter.stream(
            {
                'id': provider,
                'usage': {
                    'prompt_tokens': 11,
                    'completion_tokens': 4,
                    'cost': cost,
                },
            },
            __metadata__=metadata,
        )

    async def test_request_is_durable_before_upstream_usage_exists(self):
        metadata = await self.start_request()
        async with await tracker.database() as db:
            row = (await db.execute(text('SELECT amount,detail FROM zen_user_cost_entry'))).mappings().one()
        self.assertIsNone(row['amount'])
        self.assertEqual(json.loads(row['detail'])['state'], 'pending')
        self.assertIn('待核对 1 条', await self.filter.report(self.user_a))
        self.assertNotIn('not-a-real-key', json.dumps(metadata))

    async def test_stream_amount_survives_missing_chat_and_outlet(self):
        metadata = await self.start_request()
        await self.stream_cost(metadata)
        # No chat table is present and no completion callback is run.
        result = await self.filter.report(self.user_a)
        self.assertIn('上游费用 $0.10000000', result)
        self.assertIn('待核对 1 条', result)
        self.assertNotIn('Alice', await self.filter.report(self.user_b))

    async def test_duplicate_usage_frames_do_not_duplicate_persisted_amount(self):
        metadata = await self.start_request()
        await self.stream_cost(metadata)
        await self.stream_cost(metadata)
        async with await tracker.database() as db:
            self.assertEqual((await db.execute(text('SELECT count(*) FROM zen_user_cost_call'))).scalar(), 1)
        result = await self.filter.report(self.user_a)
        self.assertIn('上游费用 $0.10000000', result)
        self.assertNotIn('$0.20000000', result)

    async def test_tool_rounds_are_checkpoints_not_additional_completed_replies(self):
        metadata = await self.start_request()
        await self.stream_cost(metadata)
        await self.filter.request({'stream': True}, __metadata__=metadata, __user__=self.user_a)
        await self.stream_cost(metadata, 'call-2', 0.2)
        result = await self.filter.report(self.user_a)
        self.assertIn('上游费用 $0.30000000', result)
        body = copy.deepcopy(self.body)
        body['messages'][0]['usage']['cost'] = 0.3
        await self.record(self.user_a, metadata, body)
        result = await self.filter.report(self.user_a)
        self.assertIn('上游费用 $0.30000000', result)
        self.assertIn('待核对 0 条', result)
        self.assertNotIn('$0.60000000', result)

    async def test_unknown_later_round_keeps_known_cost_and_pending_marker(self):
        metadata = await self.start_request()
        await self.stream_cost(metadata)
        await self.filter.request({'stream': True}, __metadata__=metadata, __user__=self.user_a)
        body = copy.deepcopy(self.body)
        body['messages'][0]['usage'] = {'incomplete': True}
        await self.record(self.user_a, metadata, body)
        result = await self.filter.report(self.user_a)
        self.assertIn('上游费用 $0.10000000', result)
        self.assertIn('待核对 1 条', result)

    async def test_cancelled_stream_wait_does_not_cancel_its_database_checkpoint(self):
        metadata = await self.start_request()
        started, proceed = asyncio.Event(), asyncio.Event()
        original = self.filter.checkpoint

        async def delayed(*args):
            started.set()
            await proceed.wait()
            await original(*args)

        with patch.object(self.filter, 'checkpoint', delayed):
            outer = asyncio.create_task(self.stream_cost(metadata))
            await asyncio.wait_for(started.wait(), timeout=2)
            outer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await outer
            proceed.set()
            await asyncio.wait_for(asyncio.gather(*self.filter._writes), timeout=2)
        self.assertIn('上游费用 $0.10000000', await self.filter.report(self.user_a))

    async def test_pending_request_survives_new_filter_instance(self):
        await self.start_request()
        fresh = tracker.Filter()
        self.assertIn('待核对 1 条', await fresh.report(self.user_a))

    async def test_database_failure_blocks_the_request_before_upstream_dispatch(self):
        with patch.object(self.filter, 'begin_attempt', AsyncMock(side_effect=OSError('offline'))):
            with self.assertRaisesRegex(RuntimeError, '已阻止调用'):
                await self.start_request()

    async def test_late_stream_frame_cannot_overwrite_final_reconciled_amount(self):
        metadata = await self.start_request()
        await self.stream_cost(metadata)
        body = copy.deepcopy(self.body)
        body['messages'][0]['usage']['cost'] = 0.3
        await self.record(self.user_a, metadata, body)
        await self.stream_cost(metadata, cost=0.9)
        result = await self.filter.report(self.user_a)
        self.assertIn('上游费用 $0.30000000', result)
        self.assertIn('待核对 0 条', result)

    async def test_first_request_hook_reuses_the_inlet_attempt(self):
        metadata = await self.start_request()
        await self.filter.request({'stream': True}, __metadata__=metadata, __user__=self.user_a)
        async with await tracker.database() as db:
            self.assertEqual((await db.execute(text('SELECT count(*) FROM zen_user_cost_call'))).scalar(), 1)

    async def test_incomplete_responses_event_keeps_its_observed_cost(self):
        metadata = await self.start_request()
        await self.filter.stream(
            {
                'type': 'response.incomplete',
                'response': {
                    'id': 'response-1',
                    'usage': {'input_tokens': 11, 'output_tokens': 4, 'cost': 0.1},
                },
            },
            __metadata__=metadata,
        )
        result = await self.filter.report(self.user_a)
        self.assertIn('上游费用 $0.10000000', result)
        self.assertIn('待核对 1 条', result)

    async def test_usage_without_provider_id_still_creates_checkpoint(self):
        metadata = await self.start_request()
        await self.filter.stream({'usage': {'cost': 0.1}}, __metadata__=metadata)
        self.assertIn('上游费用 $0.10000000', await self.filter.report(self.user_a))

    async def test_anthropic_partial_usage_is_merged_for_the_same_call(self):
        metadata = await self.start_request()
        await self.filter.stream(
            {
                'type': 'message_start',
                'message': {
                    'id': 'message-1',
                    'usage': {'input_tokens': 11},
                },
            },
            __metadata__=metadata,
        )
        await self.filter.stream(
            {'type': 'message_delta', 'usage': {'output_tokens': 4, 'cost': 0.1}}, __metadata__=metadata
        )
        self.assertEqual(metadata['zen_cost_calls']['message-1']['input_tokens'], 11)
        self.assertEqual(metadata['zen_cost_calls']['message-1']['output_tokens'], 4)
        self.assertIn('上游费用 $0.10000000', await self.filter.report(self.user_a))


if __name__ == '__main__':
    unittest.main()
