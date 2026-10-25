"""按 JSON Schema 校验评测输出（轻量实现，不依赖 jsonschema）。

用法：
    python scripts/validate_schema.py prompts/example-output-v1.1-good.json
    python scripts/validate_schema.py prompts/example-output-v1.1-good.json --schema prompts/output-schema-v1.1.json

支持：required / type / enum / min-max / minItems-maxItems / items / properties / $ref(#/$defs)
不支持的字段会被忽略，不会误报。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


def err(path: str, msg: str) -> str:
    return f"{path or '$'}: {msg}"


def check_schema(node, schema: dict, root: dict, path: str, out: list[str]) -> None:
    if not isinstance(schema, dict):
        return
    if "$ref" in schema:
        ref = schema["$ref"]
        target = root
        for part in ref.lstrip("#/").split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        check_schema(node, target, root, path, out)
        return

    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if node is None and "null" in types:
            return
        # 联合类型必须逐个尝试，不能只检查第一个。bool 是 int 的子类，需显式排除。
        def _matches(name: str) -> bool:
            want = TYPES.get(name)
            if not want:
                return False
            numeric = name in {"integer", "number"}
            return isinstance(node, want) and not (numeric and isinstance(node, bool))

        if not any(_matches(t) for t in types):
            out.append(err(path, f"类型应为 {schema['type']}，实际 {type(node).__name__}"))
            return

    if "const" in schema and node != schema["const"]:
        out.append(err(path, f"取值 {node!r} 必须等于 {schema['const']!r}"))

    if "enum" in schema and node not in schema["enum"]:
        out.append(err(path, f"取值 {node!r} 不在 {schema['enum']}"))

    if isinstance(node, str):
        if "minLength" in schema and len(node) < schema["minLength"]:
            out.append(err(path, f"长度 {len(node)} < minLength {schema['minLength']}"))
        if "maxLength" in schema and len(node) > schema["maxLength"]:
            out.append(err(path, f"长度 {len(node)} > maxLength {schema['maxLength']}"))

    if isinstance(node, (int, float)) and not isinstance(node, bool):
        if "minimum" in schema and node < schema["minimum"]:
            out.append(err(path, f"{node} < minimum {schema['minimum']}"))
        if "maximum" in schema and node > schema["maximum"]:
            out.append(err(path, f"{node} > maximum {schema['maximum']}"))

    if isinstance(node, list):
        if "minItems" in schema and len(node) < schema["minItems"]:
            out.append(err(path, f"数组长度 {len(node)} < minItems {schema['minItems']}"))
        if "maxItems" in schema and len(node) > schema["maxItems"]:
            out.append(err(path, f"数组长度 {len(node)} > maxItems {schema['maxItems']}"))
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(node):
                check_schema(item, schema["items"], root, f"{path}[{i}]", out)

    if isinstance(node, dict):
        for key in schema.get("required", []):
            if key not in node:
                out.append(err(f"{path}.{key}", "缺少必填字段"))
        for key, sub in (schema.get("properties") or {}).items():
            if key in node:
                check_schema(node[key], sub, root, f"{path}.{key}" if path else key, out)


def main() -> int:
    ap = argparse.ArgumentParser(description="按 schema 校验评测输出")
    ap.add_argument("files", nargs="+", help="待校验的 JSON 文件")
    ap.add_argument("--schema", default="prompts/output-schema-v1.1.json")
    args = ap.parse_args()

    schema_path = Path(args.schema)
    if not schema_path.exists():
        print(f"[error] 找不到 schema：{schema_path}", file=sys.stderr)
        return 2
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    bad = 0
    for f in args.files:
        p = Path(f)
        if not p.exists():
            print(f"[skip] {f} 不存在", file=sys.stderr)
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        problems: list[str] = []
        check_schema(data, schema, schema, "", problems)
        if problems:
            bad += 1
            print(f"[FAIL] {p.name}")
            for msg in problems:
                print(f"   - {msg}")
        else:
            print(f"[PASS] {p.name}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
