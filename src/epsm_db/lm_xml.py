from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: ET.Element, name: str) -> str | None:
    for child in element.iter():
        if local_name(child.tag) == name and child.text and child.text.strip():
            return child.text.strip()
    return None


def _integer(element: ET.Element, name: str) -> int | None:
    value = _text(element, name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _boolean(element: ET.Element, name: str) -> bool | None:
    value = _text(element, name)
    if value is None:
        return None
    return {"true": True, "1": True, "false": False, "0": False}.get(value.casefold())


def flattened_values(element: ET.Element) -> dict[str, list[str]]:
    """Lossless textual projection: repeated paths remain repeated values."""
    values: dict[str, list[str]] = {}

    def visit(node: ET.Element, prefix: str) -> None:
        name = local_name(node.tag)
        path = f"{prefix}/{name}" if prefix else name
        if node.text and node.text.strip():
            values.setdefault(path, []).append(node.text)
        for name, value in node.attrib.items():
            values.setdefault(path + "/@" + name, []).append(value)
        for child in node:
            visit(child, path)

    visit(element, "")
    return values


def parse_building_element(
    element: ET.Element, *, source_file: str, source_record: int
) -> dict[str, Any]:
    building_id = _text(element, "objektidentitet")
    purposes: list[dict[str, Any]] = []
    entrances: list[dict[str, Any]] = []
    purpose_types = {
        "Bostad",
        "Industri",
        "Samhällsfunktion",
        "Verksamhet",
        "Ekonomibyggnad",
        "Komplementbyggnad",
        "ÖvrigByggnad",
        "OvrigByggnad",
    }
    for child in element:
        kind = local_name(child.tag)
        if kind == "Entre":
            entrances.append(
                {
                    "entrance_id": _text(child, "objektidentitet"),
                    "object_version": _integer(child, "objektversion"),
                    "valid_from": _text(child, "versionGiltigFran"),
                    "object_status": _text(child, "objektstatus"),
                    "entrance_type": _text(child, "entretyp"),
                    "address_place_id": _text(child, "adressplats"),
                    "original_values": flattened_values(child),
                }
            )
        elif kind in purpose_types:
            purposes.append(
                {
                    "category": kind,
                    "detail": _text(child, "andamal"),
                    "is_main": _boolean(child, "huvudandamal"),
                    "dwelling_area_m2": _text(child, "totalBoarea"),
                    "original_values": flattened_values(child),
                }
            )
    return {
        "building_id": building_id,
        "object_status": _text(element, "objektstatus"),
        "object_version": _integer(element, "objektversion"),
        "valid_from": _text(element, "versionGiltigFran"),
        "house_number": _integer(element, "husnummer"),
        "construction_year": _integer(element, "nybyggnadsar"),
        "construction_year_raw": _text(element, "nybyggnadsar"),
        "extension_year": _integer(element, "tillbyggnadsar"),
        "extension_year_raw": _text(element, "tillbyggnadsar"),
        "main_building": _boolean(element, "huvudbyggnad"),
        "source_file": source_file,
        "source_record": source_record,
        "original_values": flattened_values(element),
        "purposes": purposes,
        "entrances": entrances,
    }


def parse_address_element(
    element: ET.Element, *, source_file: str, source_record: int
) -> dict[str, Any]:
    points = [p for p in element.iter() if local_name(p.tag) == "Point"]
    point = points[0] if len(points) == 1 else None
    position = _text(point, "pos") if point is not None else None
    easting = northing = None
    if position:
        crs = str(point.get("srsName", ""))
        if not crs.endswith("3006"):
            raise ValueError("Address point must explicitly declare EPSG:3006")
        values = position.split()
        if len(values) >= 2:
            # The LM GML contract uses northing easting for EPSG:3006.
            northing, easting = float(values[0]), float(values[1])
    names = [
        child.text.strip()
        for child in element.iter()
        if local_name(child.tag) == "faststalltNamn" and child.text and child.text.strip()
    ]
    return {
        "address_place_id": _text(element, "objektidentitet"),
        "object_version": _integer(element, "objektversion"),
        "valid_from": _text(element, "versionGiltigFran"),
        "object_status": _text(element, "objektstatus"),
        "municipality_code": _text(element, "kommunkod"),
        "locality": _text(element, "postort"),
        "street_name": names[0] if names else None,
        "address_number": _text(element, "adressplatsnummer"),
        "easting": easting,
        "northing": northing,
        "source_file": source_file,
        "source_record": source_record,
        "original_values": flattened_values(element),
    }


def _iterparse_selected_elements(path: Path, member_name: str, identifiers: set[str]):
    record = 0
    stack = []
    for event, element in ET.iterparse(path, events=("start", "end")):
        if event == "start":
            stack.append(element)
            continue
        if local_name(element.tag) != member_name:
            stack.pop()
            continue
        record += 1
        identifier = _text(element, "objektidentitet")
        if identifiers is None or identifier in identifiers:
            yield record, element
        element.clear()
        if len(stack) > 1:
            stack[-2].remove(element)
        stack.pop()


def _selected_elements(path: Path, member_name: str, identifiers: set[str]):
    """Frame the inspected default-namespace LM XML and parse selected records only.

    Files are streamed in bounded chunks; no national XML tree is constructed.
    Other namespace layouts fall back to the bounded tree parser. Original files
    and record ordinals are unchanged, and building locators are checked by callers.
    """
    import re

    with path.open("rb") as source:
        header = source.read(4096)
        root = re.search(rb"<(?!\?)[A-Za-z][^>]*>", header)
        if root is None or re.search(rb"<[A-Za-z0-9_]+:" + member_name.encode() + rb"\b", header):
            yield from _iterparse_selected_elements(path, member_name, identifiers)
            return
        declarations = re.findall(rb'xmlns(?::[A-Za-z0-9_]+)?="[^"]*"', root.group())
        wrapper = b"<root " + b" ".join(declarations) + b">"
        opening = re.compile(rb"<" + member_name.encode() + rb"(?:\s[^>]*|)>")
        closing = b"</" + member_name.encode() + b">"
        identifier = re.compile(rb"<(?:[A-Za-z0-9_]+:)?objektidentitet[^>]*>([^<]*)<")
        source.seek(0)
        buffer = b""
        record = 0
        while True:
            chunk = source.read(4 * 1024 * 1024)
            buffer += chunk
            consumed = 0
            while match := opening.search(buffer, consumed):
                end = buffer.find(closing, match.end())
                if end < 0:
                    consumed = match.start()
                    break
                end += len(closing)
                record += 1
                fragment = buffer[match.start() : end]
                found = identifier.search(fragment)
                if identifiers is None or (found and found.group(1).decode("utf-8").strip() in identifiers):
                    element = ET.fromstring(wrapper + fragment + b"</root>")[0]
                    yield record, element
                consumed = end
            if consumed:
                buffer = buffer[consumed:]
            elif len(buffer) > 16 * 1024 * 1024:
                raise ValueError("Unsupported XML framing or excessive record size")
            if not chunk:
                if opening.search(buffer):
                    raise ValueError("Truncated XML member")
                break


def load_selected_buildings(
    locators: Iterable[Mapping[str, Any]], xml_root: Path
) -> list[dict[str, Any]]:
    by_file: dict[str, dict[str, int]] = {}
    for row in locators:
        by_file.setdefault(str(row["source_file"]), {})[str(row["building_id"])] = int(
            row["source_record"]
        )
    output: list[dict[str, Any]] = []
    for filename, expected in sorted(by_file.items()):
        path = xml_root / filename
        found: set[str] = set()
        for record, element in _selected_elements(path, "Byggnad", set(expected)):
            parsed = parse_building_element(element, source_file=filename, source_record=record)
            building_id = str(parsed["building_id"])
            if record != expected[building_id]:
                raise ValueError(
                    f"SQLite locator mismatch for {building_id}: {record} != {expected[building_id]}"
                )
            output.append(parsed)
            found.add(building_id)
        if found != set(expected):
            raise ValueError(
                f"Building XML locator did not resolve: {sorted(set(expected) - found)}"
            )
    return sorted(output, key=lambda row: str(row["building_id"]))


def load_selected_addresses(address_ids: Iterable[str], xml_root: Path) -> list[dict[str, Any]]:
    remaining = {str(value) for value in address_ids if value}
    output: list[dict[str, Any]] = []
    for path in sorted(xml_root.glob("*.xml")):
        if not remaining:
            break
        for record, element in _selected_elements(path, "Belagenhetsadress", remaining):
            parsed = parse_address_element(element, source_file=path.name, source_record=record)
            output.append(parsed)
            remaining.discard(str(parsed["address_place_id"]))
    return sorted(output, key=lambda row: str(row["address_place_id"]))
