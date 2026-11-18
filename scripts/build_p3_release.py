from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from card_studio.production.release import build_manifest, write_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--regression-passed", type=int, required=True)
    parser.add_argument("--regression-total", type=int, required=True)
    parser.add_argument("--output", type=Path, default=Path("release/p3/deployment-manifest.json"))
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest = build_manifest(root, arguments.image_digest, arguments.regression_passed, arguments.regression_total)
    write_manifest(arguments.output, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
