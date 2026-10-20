import os
from pathlib import Path
import tempfile

_TEST_ROOT = Path(tempfile.mkdtemp(prefix='sentrion-test-'))
os.environ['SENTRION_DATA_DIR'] = str(_TEST_ROOT / 'data')
os.environ['SENTRION_DB_PATH'] = str(_TEST_ROOT / 'data' / 'test.db')
os.environ['SENTRION_SESSION_SECRET'] = 'test-secret-not-for-production-0123456789'
os.environ['SENTRION_ENABLE_EXTERNAL_AI'] = 'false'

import pytest

@pytest.fixture(scope='session')
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def login_as(client, email='finance@sentrion.local', password='FinanceDemo!2026'):
    client.cookies.clear()
    response = client.get('/login')
    import re
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match, 'CSRF token missing from login form'
    response = client.post('/login', data={'csrf_token':match.group(1),'email':email,'password':password}, follow_redirects=False)
    assert response.status_code == 303 and response.headers.get('location') == '/dashboard'
    return client


def csrf_from(client, path='/dashboard'):
    import re
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match, f'CSRF token missing from {path}'
    return match.group(1)
