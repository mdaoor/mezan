"""Write openapi.json (run after each phase)."""
import json
import tempfile
from pathlib import Path

from mizan.api.app import create_app
from mizan.config import Settings

app = create_app(Settings(data_dir=Path(tempfile.mkdtemp())))
Path("openapi.json").write_text(json.dumps(app.openapi(), indent=2, ensure_ascii=False))
print("openapi.json written:", len(app.openapi()["paths"]), "paths")
