from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from .graph import run_pipeline


def emit(stage: str, detail: str) -> None:
    print(
        json.dumps(
            {
                "type": "progress",
                "stage": stage,
                "detail": detail,
                "at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def main() -> int:
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        payload = json.load(sys.stdin)
        result = run_pipeline(payload, emit)
        print(json.dumps({"type": "result", "result": result}, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"type": "error", "error": str(exc)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
