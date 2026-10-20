from __future__ import annotations
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]

def _load_env_file(path: Path):
    # Minimal .env reader using the standard library; existing environment values take precedence.
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        value = line.strip()
        if not value or value.startswith('#') or '=' not in value:
            continue
        key, raw = value.split('=', 1)
        key = key.strip(); raw = raw.strip()
        if not key or key in os.environ:
            continue
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
            raw = raw[1:-1]
        os.environ[key] = raw

_load_env_file(ROOT_DIR / '.env')

DATA_DIR = Path(os.getenv('SENTRION_DATA_DIR', ROOT_DIR / 'data')).resolve()
UPLOAD_DIR = DATA_DIR / 'uploads'
DB_PATH = Path(os.getenv('SENTRION_DB_PATH', DATA_DIR / 'sentrion.db')).resolve()
SESSION_SECRET = os.getenv('SENTRION_SESSION_SECRET', 'local-demo-only-change-before-deployment-7d2d9f2b')
SESSION_HTTPS_ONLY = os.getenv('SENTRION_HTTPS_ONLY', 'false').lower() == 'true'
MAX_UPLOAD_BYTES = int(os.getenv('SENTRION_MAX_UPLOAD_MB', '10')) * 1024 * 1024
DEFAULT_CURRENCY = os.getenv('SENTRION_DEFAULT_CURRENCY', 'USD').strip().upper()
if not (len(DEFAULT_CURRENCY) == 3 and DEFAULT_CURRENCY.isalpha()):
    DEFAULT_CURRENCY = 'USD'
APP_NAME = 'Sentrion'
APP_VERSION = '0.2.6'
EXTERNAL_AI_CONFIGURED = os.getenv('SENTRION_ENABLE_EXTERNAL_AI', 'false').lower() == 'true' and bool(os.getenv('OPENAI_API_KEY', '').strip())

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
