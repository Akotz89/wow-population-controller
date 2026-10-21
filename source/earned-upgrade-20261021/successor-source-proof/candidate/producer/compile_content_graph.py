#!/usr/bin/env python3
"""Compile a normalized effective-content extract into canonical content-graph-v1.

This stage is intentionally database-neutral. The future exact-pin extractor must
reproduce the core's DBC/world-overlay merge and emit this normalized shape. This
compiler then enforces referential, condition, availability, and determinism
invariants without touching a database or realm service.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
from pathlib import Path
from typing import Any


NODE_KINDS = {
    "quest": "quests",
    "item": "items",
    "creature": "creatures",
    "gameobject": "gameobjects",
    "spell": "spells",
    "trainer": "trainers",
    "vendor": "vendors",
    "taxi_node": "taxi_nodes",
    "area": "areas",
    "map": "maps",
    "dungeon": "dungeons",
    "loot_template": "loot_templates",
    "recipe": "recipes",
}
MAX_CONDITION_DEPTH = 128
MAX_CONDITION_NODES = 1_000_000


class CompileError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CompileError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CompileError(f"cannot read normalized extract {path}: {error}") from error
    if not isinstance(value, dict):
        raise CompileError("normalized extract root must be an object")
    return value


def _require_object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CompileError(f"{location} must be an object")
    return value


def _require_array(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise CompileError(f"{location} must be an array")
    return value


def _validate_availability(value: Any, location: str, condition_ids: set[str]) -> None:
    availability = _require_object(value, location)
    minimum = availability.get("minimum_frontier")
    maximum = availability.get("maximum_frontier")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or not isinstance(maximum, int) or isinstance(maximum, bool):
        raise CompileError(f"{location} frontier values must be integers")
    if minimum < 1 or maximum > 80 or minimum > maximum:
        raise CompileError(f"{location} has an impossible progression frontier")
    condition_id = availability.get("condition_id")
    if condition_id is not None and condition_id not in condition_ids:
        raise CompileError(f"{location} references missing condition {condition_id}")
    for key in ("event_ids", "pool_ids"):
        values = _require_array(availability.get(key), f"{location}.{key}")
        if len(values) != len(set(values)):
            raise CompileError(f"{location}.{key} contains duplicates")
        values.sort()


def _condition_references(expression: Any, location: str) -> set[str]:
    references: set[str] = set()
    stack: list[tuple[Any, int, str]] = [(expression, 1, location)]
    visited = 0
    while stack:
        current, depth, current_location = stack.pop()
        visited += 1
        if visited > MAX_CONDITION_NODES:
            raise CompileError("condition expression exceeds node bound")
        if depth > MAX_CONDITION_DEPTH:
            raise CompileError(f"{current_location} exceeds condition depth bound")
        node = _require_object(current, current_location)
        operation = node.get("op")
        if operation in ("all", "any"):
            arguments = _require_array(node.get("arguments"), f"{current_location}.arguments")
            if not arguments:
                raise CompileError(f"{current_location}.arguments must not be empty")
            for index, argument in enumerate(reversed(arguments)):
                real_index = len(arguments) - index - 1
                stack.append((argument, depth + 1, f"{current_location}.arguments[{real_index}]"))
        elif operation == "not":
            stack.append((node.get("argument"), depth + 1, f"{current_location}.argument"))
        elif operation == "reference":
            reference = node.get("condition_id")
            if not isinstance(reference, str) or not reference:
                raise CompileError(f"{current_location}.condition_id must be a non-empty string")
            references.add(reference)
        elif operation == "leaf":
            values = _require_array(node.get("values"), f"{current_location}.values")
            if len(values) != 3 or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
                raise CompileError(f"{current_location}.values must contain three unsigned integers")
        elif operation not in ("true", "false", "indeterminate"):
            raise CompileError(f"{current_location} has unsupported operation {operation!r}")
    return references


def _canonicalize_condition_expression(expression: Any, location: str, depth: int = 1) -> dict[str, Any]:
    if depth > MAX_CONDITION_DEPTH:
        raise CompileError(f"{location} exceeds condition depth bound")
    node = _require_object(expression, location)
    operation = node.get("op")
    if operation in ("all", "any"):
        if set(node) != {"op", "arguments"}:
            raise CompileError(f"{location} has unexpected condition fields")
        arguments = _require_array(node.get("arguments"), f"{location}.arguments")
        if not arguments:
            raise CompileError(f"{location}.arguments must not be empty")
        canonical = [
            _canonicalize_condition_expression(argument, f"{location}.arguments[{index}]", depth + 1)
            for index, argument in enumerate(arguments)
        ]
        canonical.sort(key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")))
        return {"op": operation, "arguments": canonical}
    if operation == "not":
        if set(node) != {"op", "argument"}:
            raise CompileError(f"{location} has unexpected condition fields")
        return {
            "op": "not",
            "argument": _canonicalize_condition_expression(node.get("argument"), f"{location}.argument", depth + 1),
        }
    if operation == "leaf":
        if set(node) != {"op", "condition_type", "values"}:
            raise CompileError(f"{location} has unexpected condition fields")
        return {"op": "leaf", "condition_type": node.get("condition_type"), "values": node.get("values")}
    if operation == "reference":
        if set(node) != {"op", "condition_id"}:
            raise CompileError(f"{location} has unexpected condition fields")
        return {"op": "reference", "condition_id": node.get("condition_id")}
    if operation in ("true", "false", "indeterminate"):
        if set(node) != {"op"}:
            raise CompileError(f"{location} has unexpected condition fields")
        return {"op": operation}
    raise CompileError(f"{location} has unsupported operation {operation!r}")


def _validate_condition_cycles(references: dict[str, set[str]]) -> None:
    indegree = {condition_id: 0 for condition_id in references}
    dependents: dict[str, list[str]] = {condition_id: [] for condition_id in references}
    for condition_id, targets in references.items():
        for target in targets:
            if target not in references:
                raise CompileError(f"condition {condition_id} references missing condition {target}")
            indegree[condition_id] += 1
            dependents[target].append(condition_id)
    ready = sorted(condition_id for condition_id, degree in indegree.items() if degree == 0)
    heapq.heapify(ready)
    visited = 0
    while ready:
        current = heapq.heappop(ready)
        visited += 1
        for dependent in sorted(dependents[current]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)
    if visited != len(references):
        raise CompileError("condition reference graph contains a cycle")


def compile_graph(source: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if source.get("schema_version") != "cpi-207-content-graph-v1":
        raise CompileError("schema_version must be cpi-207-content-graph-v1")

    conditions = _require_array(source.get("condition_roots"), "condition_roots")
    condition_ids: set[str] = set()
    condition_references: dict[str, set[str]] = {}
    for index, raw_condition in enumerate(conditions):
        condition = _require_object(raw_condition, f"condition_roots[{index}]")
        condition_id = condition.get("condition_id")
        if not isinstance(condition_id, str) or not condition_id:
            raise CompileError(f"condition_roots[{index}].condition_id must be a non-empty string")
        if condition_id in condition_ids:
            raise CompileError(f"duplicate condition ID: {condition_id}")
        condition_ids.add(condition_id)
        condition["expression"] = _canonicalize_condition_expression(
            condition.get("expression"), f"condition_roots[{index}].expression"
        )
        condition_references[condition_id] = _condition_references(
            condition.get("expression"), f"condition_roots[{index}].expression"
        )
    _validate_condition_cycles(condition_references)
    conditions.sort(key=lambda condition: condition["condition_id"])

    nodes = _require_object(source.get("nodes"), "nodes")
    node_ids: dict[str, set[int]] = {}
    for kind, collection_name in NODE_KINDS.items():
        collection = _require_array(nodes.get(collection_name), f"nodes.{collection_name}")
        seen: set[int] = set()
        for index, raw_node in enumerate(collection):
            node = _require_object(raw_node, f"nodes.{collection_name}[{index}]")
            node_id = node.get("id")
            if not isinstance(node_id, int) or isinstance(node_id, bool) or node_id < 0 or node_id > 4_294_967_295:
                raise CompileError(f"nodes.{collection_name}[{index}].id is outside uint32")
            if node_id in seen:
                raise CompileError(f"duplicate {kind} ID: {node_id}")
            seen.add(node_id)
            _validate_availability(node.get("availability"), f"nodes.{collection_name}[{index}].availability", condition_ids)
        collection.sort(key=lambda node: node["id"])
        node_ids[kind] = seen

    edges = _require_array(source.get("edges"), "edges")
    edge_ids: set[str] = set()
    for index, raw_edge in enumerate(edges):
        edge = _require_object(raw_edge, f"edges[{index}]")
        edge_id = edge.get("edge_id")
        if not isinstance(edge_id, str) or not edge_id:
            raise CompileError(f"edges[{index}].edge_id must be a non-empty string")
        if edge_id in edge_ids:
            raise CompileError(f"duplicate edge ID: {edge_id}")
        edge_ids.add(edge_id)
        for endpoint_name in ("from", "to"):
            endpoint = _require_object(edge.get(endpoint_name), f"edges[{index}].{endpoint_name}")
            kind = endpoint.get("kind")
            node_id = endpoint.get("id")
            if kind not in node_ids or node_id not in node_ids[kind]:
                raise CompileError(f"edge {edge_id} has missing {endpoint_name} endpoint {kind}:{node_id}")
        _validate_availability(edge.get("availability"), f"edges[{index}].availability", condition_ids)
    edges.sort(key=lambda edge: edge["edge_id"])

    canonical_bytes = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return source, hashlib.sha256(canonical_bytes).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--digest-output", type=Path)
    arguments = parser.parse_args()

    graph, digest = compile_graph(load_json(arguments.input))
    payload = json.dumps(graph, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    arguments.output.write_bytes(payload)
    if arguments.digest_output is not None:
        arguments.digest_output.write_text(digest + "\n", encoding="ascii")
    print(digest)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CompileError as error:
        raise SystemExit(f"content graph compilation failed: {error}") from error
