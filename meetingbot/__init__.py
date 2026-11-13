"""Local Telegram meeting coordinator."""
from hashlib import sha256
from pathlib import Path

COORDINATOR_VERSION = '0.3.1'


def source_revision():
    root = Path(__file__).parent
    digest = sha256()
    for name in ('__init__.py', '__main__.py', 'ai.py', 'engine.py', 'models.py', 'store.py', 'telegram.py'):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()[:12]


LOADED_REVISION = source_revision()
