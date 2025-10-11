"""Generate the raw OpenAPI schema directly from the FastAPI application."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.main import app  # noqa: E402


OUTPUT_FILE = Path("openapi_raw.json")


def main() -> None:
    schema = app.openapi()
    OUTPUT_FILE.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
