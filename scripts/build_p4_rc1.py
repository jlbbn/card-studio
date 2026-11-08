from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from card_studio.p4.manifests import build_rc1_manifest, validate_rc1_manifest, write_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--rollback-digest", required=True)
    parser.add_argument("--regression-passed", type=int, required=True)
    parser.add_argument("--regression-total", type=int, required=True)
    arguments = parser.parse_args()
    manifest = build_rc1_manifest(
        ROOT, arguments.image_digest, arguments.rollback_digest,
        arguments.regression_passed, arguments.regression_total,
    )
    if not validate_rc1_manifest(manifest):
        return 2
    write_json(ROOT / "release/p4/rc1/deployment-manifest.json", manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
