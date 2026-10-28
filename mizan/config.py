from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _b(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("MIZAN_DATA_DIR", "./data")))
    database_url: str = ""
    secret: str = field(default_factory=lambda: os.environ.get("MIZAN_SECRET", "dev-secret-change-me-please-32bytes"))
    token_ttl_h: int = field(default_factory=lambda: int(os.environ.get("MIZAN_TOKEN_TTL_H", "24")))
    admin_email: str = field(default_factory=lambda: os.environ.get("MIZAN_ADMIN_EMAIL", "admin@mizan.local"))
    admin_password: str = field(default_factory=lambda: os.environ.get("MIZAN_ADMIN_PASSWORD", "admin"))
    plugins_file: str = field(default_factory=lambda: os.environ.get("MIZAN_PLUGINS_FILE", "plugins.yaml"))
    embedded_worker: bool = field(default_factory=lambda: _b("MIZAN_EMBEDDED_WORKER", True))
    worker_slots: int = field(default_factory=lambda: int(os.environ.get("MIZAN_WORKER_SLOTS", "4")))
    claim_ttl_min: int = field(default_factory=lambda: int(os.environ.get("MIZAN_CLAIM_TTL_MIN", "60")))
    auto_create_tables: bool = field(default_factory=lambda: _b("MIZAN_AUTO_CREATE", True))
    redis_url: str = field(default_factory=lambda: os.environ.get("MIZAN_REDIS_URL", ""))
    web_dir: str = field(default_factory=lambda: os.environ.get("MIZAN_WEB_DIR", "web/dist"))
    cors_origins: list[str] = field(default_factory=lambda: [o for o in os.environ.get("MIZAN_CORS", "").split(",") if o])

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)
        if not self.database_url:
            self.database_url = os.environ.get(
                "MIZAN_DATABASE_URL", f"sqlite+aiosqlite:///{(self.data_dir / 'mizan.db').resolve()}")

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"
