"""Run with the application's Python dependencies, never the production ledger."""

import copy
import json
import unittest
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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


if __name__ == '__main__':
    unittest.main()
