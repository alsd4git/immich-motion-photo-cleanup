"""Bounded, read-only sidecar fingerprints and XML content comparison."""

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def read_sidecar(path):
    before = path.stat()
    if before.st_size > 16 * 1024**2:
        raise ValueError("sidecar_exceeds_16_mib")
    with path.open("rb") as source:
        content = source.read(16 * 1024**2 + 1)
    if path.stat() != before or len(content) != before.st_size:
        raise ValueError("sidecar_changed_during_read")
    return content


def xml_fingerprint(content):
    # No entity expansion, external resources, filename assumptions or value
    # dropping. Attribute order/indentation differences alone are normalized.
    try:
        content = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
        return None
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        return None

    def normalized(element):
        return [
            element.tag,
            sorted(element.attrib.items()),
            (element.text or "").strip(),
            (element.tail or "").strip(),
            [normalized(child) for child in element],
        ]

    try:
        return hashlib.sha256(
            json.dumps(normalized(root), ensure_ascii=True).encode()
        ).hexdigest()
    except RecursionError:
        return None


def manifests(values, paths):
    result = []
    for value in values:
        content = read_sidecar(paths.resolve(Path(value)))
        result.append(
            {
                "path": value,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "xml_sha256": xml_fingerprint(content),
            }
        )
    return result


def compare(video, photo):
    if not video:
        return "no_video_sidecar"
    if not photo:
        return "photo_sidecar_missing"
    if {r["sha256"] for r in video} == {r["sha256"] for r in photo}:
        return "byte_identical"
    if all(r["xml_sha256"] for r in video + photo) and {
        r["xml_sha256"] for r in video
    } == {r["xml_sha256"] for r in photo}:
        return "xml_identical"
    return "different_preserved_in_backup"
