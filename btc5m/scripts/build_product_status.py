#!/usr/bin/env python3
"""Build the dashboard status JSON and authoritative Markdown from one source."""
from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_arb.product_status import (  # noqa: E402
    build_product_status,
    render_markdown,
    write_json,
)


def _write_text(text: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=datetime.now(ZoneInfo("America/Vancouver")).date().isoformat(),
    )
    parser.add_argument("--timezone", default="America/Vancouver")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args()
    json_output = args.json_output or ROOT / "state" / f"product_status_{args.date}.json"
    markdown_output = args.markdown_output or ROOT / "docs" / f"product_status_{args.date}.md"
    report = build_product_status(
        ROOT,
        evidence_date=args.date,
        timezone_name=args.timezone,
    )
    write_json(report, json_output)
    _write_text(render_markdown(report, output=markdown_output, root=ROOT), markdown_output)
    print(f"{report['overall_status']}: {report['decision']}")
    print(f"gates={len(report['gates'])} counts={report['gate_counts']}")
    print(f"json={json_output}")
    print(f"markdown={markdown_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
