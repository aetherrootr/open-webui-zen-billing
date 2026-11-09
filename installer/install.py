"""Argo CD PostSync installer; never persists or prints an API credential."""

import asyncio
import os
from datetime import timedelta
from pathlib import Path

import requests


async def install():
    root = Path(os.environ.get('BILLING_PLUGIN_DIR', Path(__file__).resolve().parent.parent / 'plugins'))
    # A mounted existing admin token can be used outside the Open WebUI image.
    token_file = os.environ.get('WEBUI_ADMIN_TOKEN_FILE')
    if token_file:
        token = Path(token_file).read_text().strip()
    else:
        os.environ['WEBUI_SECRET_KEY'] = (
            Path(os.environ.get('WEBUI_SECRET_KEY_FILE', '/app/backend/data/.webui_secret_key')).read_text().strip()
        )
        from open_webui.models.users import Users
        from open_webui.utils.auth import create_token

        users = (await Users.get_users())['users']
        admin = next((u for u in users if u.role == 'admin'), None)
        if admin is None:
            raise RuntimeError('An existing Open WebUI administrator is required')
        token = create_token({'id': admin.id}, timedelta(minutes=5))
    base = os.environ.get('WEBUI_INTERNAL_URL', 'http://open-webui:8080').rstrip('/')
    session = requests.Session()
    session.headers['Authorization'] = 'Bearer ' + token

    def api(method, path, **kwargs):
        response = session.request(method, base + path, timeout=90, allow_redirects=False, **kwargs)
        if response.status_code >= 300:
            raise RuntimeError(f'Open WebUI {method} {path} failed: HTTP {response.status_code}')
        return response.json()

    existing = {f['id']: f for f in api('GET', '/api/v1/functions/')}
    for identifier, name in [('zen_cost_tracker', 'Zen 用户费用统计'), ('zen_cost_report', 'Zen 费用报表')]:
        content = (root / (identifier + '.py')).read_text()
        form = {
            'id': identifier,
            'name': name,
            'content': content,
            'meta': {'description': 'Managed by the open-webui-zen-billing container image'},
            'commit_message': 'Synchronize Zen cost tracking from Git',
        }
        if identifier in existing:
            current = api('GET', '/api/v1/functions/id/' + identifier)
            if current['content'] != content:
                current = api('POST', f'/api/v1/functions/id/{identifier}/update', json=form)
        else:
            current = api('POST', '/api/v1/functions/create', json=form)
        if not current['is_active']:
            current = api('POST', f'/api/v1/functions/id/{identifier}/toggle')
        if identifier == 'zen_cost_tracker' and not current['is_global']:
            current = api('POST', f'/api/v1/functions/id/{identifier}/toggle/global')
        assert current['is_active'] and (identifier != 'zen_cost_tracker' or current['is_global'])
        print(f'Installed and verified: {identifier}', flush=True)
    # Warm the shared model list after adding the reporting pipe.
    models = api('GET', '/api/models')['data']
    assert any(m['id'] == 'zen_cost_report' for m in models), 'Reporting model not discoverable'
    print('Zen cost plugins are ready', flush=True)


if __name__ == '__main__':
    asyncio.run(install())
