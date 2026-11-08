from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .codec import encode_tlv
from .model import TlvNode


def safe_tlv_evidence(nodes: Iterable[TlvNode]) -> list[dict[str, Any]]:
    evidence = []
    for node in nodes:
        item: dict[str, Any] = {
            "tag": node.tag,
            "known_lab_tag": node.known,
            "constructed": node.constructed,
            "offset": node.offset,
            "depth": node.depth,
            "context": node.context,
            "value_length": len(encode_tlv(node.children) if node.constructed else node.value),
        }
        if node.constructed:
            item["children"] = safe_tlv_evidence(node.children)
        evidence.append(item)
    return evidence
