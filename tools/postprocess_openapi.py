"""Post-process the generated OpenAPI schema to satisfy OpenAI Actions requirements."""
from __future__ import annotations

import argparse
import copy
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

OPENAI_OPENAPI_VERSION = "3.1.0"
PRODUCTION_SERVER_URL = "https://vpn-gpt.store"
HTTP_METHODS = {"get", "put", "post", "delete", "patch", "options", "head"}


def pascal_case(segment: str) -> str:
    words = re.split(r"[\s_\-]+", segment)
    return "".join(word.capitalize() for word in words if word)


def pascal_case_from_path(path: str, router_name: str) -> str:
    segments = [seg for seg in path.strip("/").split("/") if seg]
    normalised_router = router_name.lower()
    filtered: list[str] = []
    for seg in segments:
        raw = seg.strip("{}")
        if raw.lower() == normalised_router:
            continue
        filtered.append(raw)
    parts = [pascal_case(seg) for seg in filtered if seg]
    return "".join(parts)


def inline_parameters(schema: dict[str, Any]) -> None:
    components = schema.get("components", {})
    parameters: dict[str, Any] = components.get("parameters", {})

    def _resolve(param: dict[str, Any]) -> dict[str, Any]:
        ref = param.get("$ref")
        if not ref:
            return param
        if not ref.startswith("#/components/parameters/"):
            return param
        key = ref.rsplit("/", 1)[-1]
        target = parameters.get(key)
        if not target:
            raise KeyError(f"Missing parameter component for {ref}")
        return copy.deepcopy(target)

    for path_item in schema.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        if "parameters" in path_item:
            path_item["parameters"] = [_resolve(p) for p in path_item.get("parameters", [])]
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                continue
            if "parameters" in operation:
                operation["parameters"] = [_resolve(p) for p in operation.get("parameters", [])]


def ensure_tags(schema: dict[str, Any]) -> None:
    for path, path_item in schema.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            tags = operation.get("tags")
            if tags:
                continue
            if path.startswith("/health"):
                operation["tags"] = ["health"]
            elif path.startswith("/vpn"):
                operation["tags"] = ["vpn"]
            elif path.startswith("/users"):
                operation["tags"] = ["users"]
            elif path.startswith("/notify"):
                operation["tags"] = ["notify"]
            elif path.startswith("/admin"):
                operation["tags"] = ["admin"]


def ensure_operation_ids(schema: dict[str, Any]) -> None:
    existing: set[str] = set()
    for path, path_item in schema.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            tags: Iterable[str] = operation.get("tags", []) or []
            router_name = str(next(iter(tags), "default")).lower()
            pascal = pascal_case_from_path(path, router_name)
            method_title = method.capitalize()
            base_id = f"{router_name}{method_title}{pascal}" if pascal else f"{router_name}{method_title}"
            operation_id = base_id
            if operation_id in existing:
                suffix = 2
                while f"{operation_id}{suffix}" in existing:
                    suffix += 1
                operation_id = f"{operation_id}{suffix}"
            operation["operationId"] = operation_id
            existing.add(operation_id)


def add_examples(schema: dict[str, Any]) -> None:
    paths = schema.setdefault("paths", {})
    issue = paths.get("/vpn/issue_key", {})
    issue_post = issue.get("post")
    if isinstance(issue_post, dict):
        request_body = issue_post.setdefault("requestBody", {}).setdefault("content", {}).setdefault(
            "application/json", {}
        )
        request_body.setdefault(
            "example",
            {
                "username": "demo_user",
                "days": 30,
            },
        )
        responses = issue_post.setdefault("responses", {})
        success = responses.setdefault("200", {}).setdefault("content", {}).setdefault("application/json", {})
        success.setdefault(
            "example",
            {
                "ok": True,
                "uuid": "0b5c3fce-8c1c-4ed7-8aa7-7f5c6ec0e123",
                "link": "vless://...",
                "expires": "2024-12-31",
                "message": "Ключ создан успешно.",
            },
        )

    disable = paths.get("/vpn/disable_key", {})
    disable_post = disable.get("post")
    if isinstance(disable_post, dict):
        request_body = disable_post.setdefault("requestBody", {}).setdefault("content", {}).setdefault(
            "application/json", {}
        )
        request_body.setdefault(
            "example",
            {
                "uuid": "0b5c3fce-8c1c-4ed7-8aa7-7f5c6ec0e123",
            },
        )
        responses = disable_post.setdefault("responses", {})
        success = responses.setdefault("200", {}).setdefault("content", {}).setdefault("application/json", {})
        success.setdefault(
            "example",
            {
                "ok": True,
                "uuid": "0b5c3fce-8c1c-4ed7-8aa7-7f5c6ec0e123",
            },
        )


def postprocess(schema: dict[str, Any]) -> dict[str, Any]:
    schema["openapi"] = OPENAI_OPENAPI_VERSION
    schema["servers"] = [{"url": PRODUCTION_SERVER_URL}]

    inline_parameters(schema)
    ensure_tags(schema)
    ensure_operation_ids(schema)
    add_examples(schema)
    return schema


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post-process OpenAPI schema for OpenAI Actions")
    parser.add_argument("input", type=Path, help="Path to the raw OpenAPI JSON file")
    parser.add_argument("output", type=Path, help="Path where the processed OpenAPI JSON will be written")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    processed = postprocess(raw)
    Path(args.output).write_text(json.dumps(processed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
