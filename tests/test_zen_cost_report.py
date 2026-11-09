import sys
import types
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import zen_cost_report as report


class QueryTests(unittest.TestCase):
    def test_supported_queries(self):
        cases = {
            '本月': ('current', None),
            '查看本月费用': ('current', None),
            '上月': ('previous', None),
            '查看上月费用': ('previous', None),
            '2026-11': ('month', '2026-11'),
            '查看 2026-11 费用': ('month', '2026-11'),
            '/prompt 本月': ('current', None),
            '/prompt 上月': ('previous', None),
            '/prompt 2026-11': ('month', '2026-11'),
            '/zen-billing 2026-11': ('month', '2026-11'),
            '查看累计费用': ('all', None),
            '/prompt 总费用': ('all', None),
            '最近30天': ('recent', 30),
            '2026-11-01 至 2026-11-10': ('range', ('2026-11-01', '2026-11-10')),
            '/prompt 2026-11-01 2026-11-10': ('range', ('2026-11-01', '2026-11-10')),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(report.parse_query(query), expected)

    def test_unknown_and_invalid_queries_show_help(self):
        for query in [
            '',
            '你好',
            'help',
            '帮助',
            '/prompt',
            '/zen-billing',
            '/promptfoo',
            '2026-00',
            '2026-13',
            '2026-1',
            '/prompt 2026-13',
            '乱写 2026-11 不要查询',
            '2026-02-30 至 2026-03-01',
            '2026-11-10 至 2026-11-01',
            '最近0天',
            '最近3661天',
        ]:
            with self.subTest(query=query):
                self.assertEqual(report.parse_query(query), ('help', None))

    def test_multimodal_text_extraction(self):
        self.assertEqual(
            report.last_text(
                [
                    {
                        'role': 'user',
                        'content': [
                            {'type': 'image_url', 'image_url': {}},
                            {'type': 'text', 'text': '本月'},
                        ],
                    }
                ]
            ),
            '本月',
        )


class PipeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tracker = SimpleNamespace(
            report=AsyncMock(return_value='report'), valves=SimpleNamespace(timezone='Asia/Shanghai')
        )
        self.loader = AsyncMock(return_value=(self.tracker, None, None))
        plugin = types.ModuleType('open_webui.utils.plugin')
        plugin.get_function_module_from_cache = self.loader
        self.modules = patch.dict(
            sys.modules,
            {
                'open_webui': types.ModuleType('open_webui'),
                'open_webui.utils': types.ModuleType('open_webui.utils'),
                'open_webui.utils.plugin': plugin,
            },
        )
        self.modules.start()
        self.user = {'id': 'alice', 'role': 'user'}

    async def asyncTearDown(self):
        self.modules.stop()

    async def call(self, content):
        return await report.Pipe().pipe({'messages': [{'role': 'user', 'content': content}]}, self.user, 'request')

    async def test_arbitrary_input_returns_help_without_querying_database(self):
        self.assertIn('查询帮助', await self.call('随便什么东西'))
        self.loader.assert_not_called()

    async def test_slash_help_does_not_query_or_load_tracker(self):
        self.assertIn('/prompt 2026-11', await self.call('/prompt'))
        self.loader.assert_not_called()

    async def test_month_query_keeps_authenticated_identity(self):
        self.assertEqual(await self.call('/prompt 2026-11'), 'report')
        self.tracker.report.assert_awaited_once_with(self.user, '2026-11')

    async def test_previous_month_handles_year_boundary(self):
        with patch.object(report, 'datetime') as clock:
            clock.now.return_value = datetime(2027, 1, 1)
            await self.call('上月')
        self.tracker.report.assert_awaited_once_with(self.user, '2026-12')

    async def test_unauthenticated_requests_are_rejected(self):
        result = await report.Pipe().pipe({'messages': []})
        self.assertEqual(result, '请先登录。')
        self.loader.assert_not_called()

    async def test_all_time_query_keeps_identity(self):
        await self.call('/prompt 总费用')
        self.tracker.report.assert_awaited_once_with(self.user, all_time=True)

    async def test_date_range_query_keeps_identity(self):
        await self.call('2026-11-01 至 2026-11-10')
        self.tracker.report.assert_awaited_once_with(self.user, start_date='2026-11-01', end_date='2026-11-10')

    async def test_recent_days_include_today(self):
        with patch.object(report, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 11, 10)
            await self.call('最近7天')
        self.tracker.report.assert_awaited_once_with(self.user, start_date='2026-11-04', end_date='2026-11-10')
