import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import install


class Response:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return copy.deepcopy(self.data)


class Session:
    def __init__(self):
        self.headers = {}
        self.functions = {}
        self.models = {}
        self.prompts = {}
        self.writes = []

    def request(self, method, url, **kwargs):
        path = url.removeprefix('http://webui')
        if method == 'GET' and path == '/api/v1/functions/':
            return Response(list(self.functions.values()))
        if method == 'GET' and path == '/api/models':
            return Response({'data': [{'id': 'zen_cost_report'}]})
        if method == 'GET' and path == '/api/v1/models/export':
            return Response(list(self.models.values()))
        if method == 'GET' and path == '/api/v1/prompts/':
            return Response(list(self.prompts.values()))
        if method == 'POST':
            self.writes.append(path)
        if path.startswith('/api/v1/models/'):
            form = copy.deepcopy(kwargs['json'])
            self.models[form['id']] = form
            return Response(form)
        if path.startswith('/api/v1/prompts/'):
            form = copy.deepcopy(kwargs['json'])
            if path.endswith('/create'):
                form['id'] = str(len(self.prompts) + 1)
            else:
                form['id'] = path.split('/')[5]
            self.prompts[form['command'].lstrip('/')] = form
            return Response(form)
        if path.endswith('/create'):
            form = kwargs['json']
            self.functions[form['id']] = {**form, 'is_active': False, 'is_global': False, 'valves': {'timezone': 'UTC'}}
            return Response(self.functions[form['id']])
        identifier = path.split('/')[5]
        current = self.functions[identifier]
        if path.endswith('/update'):
            current.update(kwargs['json'])
        elif path.endswith('/toggle/global'):
            current['is_global'] = not current['is_global']
        elif path.endswith('/toggle'):
            current['is_active'] = not current['is_active']
        return Response(current)


class InstallerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / 'admin-token').write_text('test-token-never-a-real-credential')
        for name in ['zen_cost_tracker', 'zen_cost_report']:
            (root / (name + '.py')).write_text('# test fixture\n')
        presets = Path(__file__).parent.parent / 'plugins/report_presets.json'
        (root / 'report_presets.json').write_text(presets.read_text())
        self.env = patch.dict(
            os.environ,
            {
                'BILLING_PLUGIN_DIR': str(root),
                'WEBUI_INTERNAL_URL': 'http://webui',
                'WEBUI_ADMIN_TOKEN_FILE': str(root / 'admin-token'),
            },
        )
        self.env.start()
        self.session = Session()
        self.mock = patch.object(install.requests, 'Session', return_value=self.session)
        self.mock.start()

    async def asyncTearDown(self):
        self.mock.stop()
        self.env.stop()
        self.temp.cleanup()

    async def test_reinstall_is_idempotent_and_preserves_valves(self):
        await install.install()
        first_writes = list(self.session.writes)
        await install.install()
        self.assertEqual(self.session.writes, first_writes)
        self.assertTrue(self.session.functions['zen_cost_tracker']['is_global'])
        self.assertTrue(self.session.functions['zen_cost_tracker']['is_active'])
        self.assertFalse(self.session.functions['zen_cost_report']['is_global'])
        self.assertEqual(self.session.functions['zen_cost_tracker']['valves'], {'timezone': 'UTC'})

    async def test_code_update_does_not_reset_configuration(self):
        await install.install()
        self.session.writes.clear()
        (Path(self.temp.name) / 'zen_cost_tracker.py').write_text('# updated fixture\n')
        await install.install()
        self.assertEqual(self.session.writes, ['/api/v1/functions/id/zen_cost_tracker/update'])
        self.assertEqual(self.session.functions['zen_cost_tracker']['valves'], {'timezone': 'UTC'})

    async def test_http_error_does_not_expose_response_body(self):
        response = Response(None)
        response.status_code = 500
        response.text = 'sensitive upstream body'
        with patch.object(self.session, 'request', return_value=response):
            with self.assertRaises(RuntimeError) as error:
                await install.install()
        self.assertIn('HTTP 500', str(error.exception))
        self.assertNotIn('sensitive', str(error.exception))

    async def test_installs_shared_presets_and_model_suggestions(self):
        await install.install()
        self.assertIn('prompt', self.session.prompts)
        self.assertIn('zen-billing-month', self.session.prompts)
        model = self.session.models['zen_cost_report']
        self.assertEqual(len(model['meta']['suggestion_prompts']), 4)
        self.assertEqual(model['access_grants'], install.PUBLIC_READ)

    async def test_does_not_overwrite_an_existing_prompt_command(self):
        existing = {'id': 'existing', 'command': 'prompt', 'name': 'My prompt', 'content': 'My content', 'meta': {}}
        self.session.prompts['prompt'] = copy.deepcopy(existing)
        await install.install()
        self.assertEqual(self.session.prompts['prompt'], existing)
        self.assertIn('zen-billing', self.session.prompts)

    async def test_preserves_model_settings_and_prompt_access_on_upgrade(self):
        await install.install()
        model = self.session.models['zen_cost_report']
        model['params'] = {'temperature': 0.2}
        model['meta']['custom_field'] = 'keep'
        model['access_grants'] = []
        preset = self.session.prompts['zen-billing-month']
        preset['access_grants'] = []
        (Path(self.temp.name) / 'report_presets.json').write_text(
            json.dumps(
                {
                    'suggestion_prompts': [{'content': '本月'}],
                    'prompts': [{'command': '/zen-billing-month', 'name': 'Updated preset', 'content': '本月'}],
                }
            )
        )
        await install.install()
        model = self.session.models['zen_cost_report']
        self.assertEqual(model['params'], {'temperature': 0.2})
        self.assertEqual(model['meta']['custom_field'], 'keep')
        self.assertEqual(model['access_grants'], [])
        self.assertEqual(self.session.prompts['zen-billing-month']['access_grants'], [])
