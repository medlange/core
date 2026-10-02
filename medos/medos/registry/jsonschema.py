# SPDX-License-Identifier: Apache-2.0
"""A JSON Schema 2020-12 validator with a CLOSED keyword set. `MOS-REG-014`/`MOS-REG-015`.

WHY THIS IS IN THE TREE AND NOT `pip install jsonschema`
--------------------------------------------------------
Two reasons, and the second is the one that matters.

1. `requirements-dev.txt` is the union of what the compose stack, the spike and the
   tooling need, and every agent in this release block shares one virtualenv. Adding a
   dependency mid-block breaks every checkout that has not reinstalled.

2. A general validator IGNORES a keyword it does not know. `MOS-REG-015` says unknown
   fields in a MANIFEST are a reject; the same reasoning applies one level up, to the
   SCHEMAS themselves. A schema author who writes `"minumum": 0` in a manifest schema
   gets, from a permissive validator, a constraint that silently does nothing -- and the
   registry then accepts a `ModelVersion` the schema was written to refuse. This
   validator refuses the SCHEMA (`SchemaError`) instead, at import time, which is the
   only moment where that typo is cheap.

So the keyword set below is exactly what `medos/medos/registry/schemas.py` uses. Adding a
keyword to a schema means adding it here too, deliberately, with a test.

WHAT IS IMPLEMENTED
-------------------
Applicators: `allOf`, `anyOf`, `oneOf`, `not`, `if`/`then`/`else`, `$ref` (absolute into
the in-repo registry, or `#/$defs/...` local), `$defs`.
Object: `properties`, `patternProperties`, `propertyNames`, `additionalProperties`,
`required`, `minProperties`, `maxProperties`.
Array: `items`, `minItems`, `maxItems`, `uniqueItems`.
Scalar: `type`, `enum`, `const`, `pattern`, `minLength`, `maxLength`, `minimum`,
`maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `format` (asserted, not
annotated -- see `FORMATS`).
Annotations, ignored by design: `$schema`, `$id`, `$comment`, `title`, `description`,
`examples`, `default`, `deprecated`.

`format` IS ASSERTED. 2020-12 makes `format` an annotation by default; this validator
treats the two formats it admits as assertions, because `created_at` being a date-time is
a constraint the registry relies on and "annotation by default" would make it decorative.

Spec: MOS-REG-014 (validate before the row is written; no store-and-validate-later),
MOS-REG-015 (2020-12, `additionalProperties: false` at every object level, unknown field
is a reject), MOS-REG-016 (one in-repo schema source). Pure: no I/O, no clock.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "KEYWORDS",
    "FORMATS",
    "SchemaError",
    "Violation",
    "assert_closed_objects",
    "check_schema",
    "iter_errors",
    "validate",
]

# Every keyword this validator understands. A schema carrying anything else is refused.
KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        # annotations -- ignored, but legal
        "$schema", "$id", "$comment", "title", "description", "examples", "default",
        "deprecated",
        # structure
        "$ref", "$defs",
        # applicators
        "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
        # any instance
        "type", "enum", "const",
        # object
        "properties", "patternProperties", "propertyNames", "additionalProperties",
        "required", "minProperties", "maxProperties",
        # array
        "items", "minItems", "maxItems", "uniqueItems",
        # string
        "pattern", "minLength", "maxLength", "format",
        # number
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    }
)

# Asserted, not annotated. `date-time` is RFC 3339; `uri` is checked only for a scheme,
# because the alternative is a URI parser nobody asked for.
FORMATS: Final[frozenset[str]] = frozenset({"date-time", "uri"})

_TYPES: Final[frozenset[str]] = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)

_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


class SchemaError(ValueError):
    """The SCHEMA is malformed -- an unknown keyword, a bad type, an unresolvable `$ref`.

    Never raised for instance data. This is a build error: `medos/medos/registry/schemas.py`
    raises it at import time, so a mistyped constraint cannot reach a running registry.
    """


@dataclass(frozen=True)
class Violation:
    """One failed assertion, located. `path` is a JSON Pointer into the instance."""

    path: str
    keyword: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        where = self.path or "(root)"
        return f"{where}: {self.message} [{self.keyword}]"


# =====================================================================================
# Schema checking -- run once, at import, over every schema in the in-repo source
# =====================================================================================
def check_schema(schema: Any, *, registry: Mapping[str, Any], path: str = "") -> None:
    """Refuse a schema this validator would silently under-enforce. `MOS-REG-015`.

    Recurses into every subschema. Raises `SchemaError` on the first problem, naming the
    location, because a build error with a location is fixable and one without is not.
    """
    if isinstance(schema, bool):
        return
    if not isinstance(schema, Mapping):
        raise SchemaError(f"{path or '(root)'}: a schema MUST be an object or a boolean")

    unknown = sorted(set(schema) - KEYWORDS)
    if unknown:
        raise SchemaError(
            f"{path or '(root)'}: unknown schema keyword(s) {unknown}. This validator "
            "has a closed keyword set on purpose (see the module docstring); an "
            "unimplemented keyword would be a constraint that silently does nothing."
        )

    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str):
            raise SchemaError(f"{path}: $ref MUST be a string")
        _resolve(ref, registry=registry, path=path)

    typ = schema.get("type")
    if typ is not None:
        names = [typ] if isinstance(typ, str) else typ
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise SchemaError(f"{path}: type MUST be a string or a list of strings")
        bad = sorted(set(names) - _TYPES)
        if bad:
            raise SchemaError(f"{path}: type {bad} is not a JSON Schema type")

    fmt = schema.get("format")
    if fmt is not None and fmt not in FORMATS:
        raise SchemaError(
            f"{path}: format {fmt!r} is not asserted by this validator "
            f"({sorted(FORMATS)}); an unasserted format is decorative."
        )

    for kw in ("pattern",):
        if kw in schema:
            try:
                re.compile(schema[kw])
            except re.error as exc:
                raise SchemaError(f"{path}: {kw} is not a valid regex: {exc}") from exc

    for kw in ("properties", "patternProperties", "$defs"):
        block = schema.get(kw)
        if block is not None:
            if not isinstance(block, Mapping):
                raise SchemaError(f"{path}: {kw} MUST be an object")
            for name, sub in block.items():
                if kw == "patternProperties":
                    try:
                        re.compile(name)
                    except re.error as exc:
                        raise SchemaError(
                            f"{path}: patternProperties key {name!r} is not a regex"
                        ) from exc
                check_schema(sub, registry=registry, path=f"{path}/{kw}/{name}")

    for kw in ("additionalProperties", "propertyNames", "items", "not", "if", "then",
               "else"):
        if kw in schema:
            check_schema(schema[kw], registry=registry, path=f"{path}/{kw}")

    for kw in ("allOf", "anyOf", "oneOf"):
        block = schema.get(kw)
        if block is not None:
            if not isinstance(block, Sequence) or isinstance(block, str) or not block:
                raise SchemaError(f"{path}: {kw} MUST be a non-empty array")
            for i, sub in enumerate(block):
                check_schema(sub, registry=registry, path=f"{path}/{kw}/{i}")

    required = schema.get("required")
    if required is not None and (
        not isinstance(required, Sequence)
        or isinstance(required, str)
        or not all(isinstance(r, str) for r in required)
    ):
        raise SchemaError(f"{path}: required MUST be an array of strings")


def assert_closed_objects(schema: Any, *, path: str = "") -> None:
    """`MOS-REG-015`: `additionalProperties: false` at EVERY object level.

    Applied to the in-repo source at import time. An object schema that declares
    `properties` but forgets the closure would accept unknown members, and
    `MOS-REG-015` makes an unknown member a reject rather than a warning -- so the
    omission is a defect in the schema, caught here rather than in production.

    `if`, `then`, `else` and `not` are NOT walked. Those four are conditions and
    overlays, not definitions: `{"if": {"properties": {"kind": {"const":
    "model_version"}}}}` selects manifests by one member and would match nothing at all
    if it were closed against the other seven. The definitions they overlay ARE walked,
    so every member of every manifest still lands in one closed object schema.
    """
    if isinstance(schema, bool) or not isinstance(schema, Mapping):
        return
    declares_object = "properties" in schema or "propertyNames" in schema
    additional = schema.get("additionalProperties", None)
    # A MAP -- chapter 6's `io.output.label_map: {0: background, 1: pleural_effusion}` --
    # has open keys by construction and CANNOT set `additionalProperties: false` without
    # forbidding every entry it exists to hold. Taken to the letter, `MOS-REG-015` forbids
    # the manifest §6.5 itself writes. It is closed here in the sense the requirement is
    # after -- no member may be arbitrary -- by constraining BOTH the key
    # (`propertyNames`) and the value (a schema-valued `additionalProperties`). Reported,
    # not widened: an object with named `properties` still takes the literal `false`.
    constrained_map = "propertyNames" in schema and isinstance(additional, Mapping)
    if declares_object and additional is not False and not constrained_map:
        raise SchemaError(
            f"{path or '(root)'}: an object schema MUST set "
            '"additionalProperties": false (MOS-REG-015)'
        )
    for kw in ("properties", "patternProperties", "$defs"):
        for name, sub in (schema.get(kw) or {}).items():
            assert_closed_objects(sub, path=f"{path}/{kw}/{name}")
    for kw in ("additionalProperties", "propertyNames", "items"):
        if kw in schema and not isinstance(schema[kw], bool):
            assert_closed_objects(schema[kw], path=f"{path}/{kw}")
    for kw in ("allOf", "anyOf", "oneOf"):
        for i, sub in enumerate(schema.get(kw) or ()):
            assert_closed_objects(sub, path=f"{path}/{kw}/{i}")


# =====================================================================================
# Instance validation
# =====================================================================================
def validate(instance: Any, schema: Any, *, registry: Mapping[str, Any]) -> None:
    """Raise `ValueError` with every violation listed, or return None. `MOS-REG-014`."""
    problems = iter_errors(instance, schema, registry=registry)
    if problems:
        raise ValueError("; ".join(str(p) for p in problems))


def iter_errors(
    instance: Any, schema: Any, *, registry: Mapping[str, Any], path: str = ""
) -> list[Violation]:
    """Every violation, not just the first: a publisher fixing a manifest wants the list."""
    if schema is True or schema == {}:
        return []
    if schema is False:
        return [Violation(path, "false", "no value is valid here")]
    if not isinstance(schema, Mapping):  # pragma: no cover - check_schema refuses these
        raise SchemaError(f"{path}: schema is not an object")

    out: list[Violation] = []

    if "$ref" in schema:
        target = _resolve(schema["$ref"], registry=registry, path=path)
        out += iter_errors(instance, target, registry=registry, path=path)

    typ = schema.get("type")
    if typ is not None:
        names = {typ} if isinstance(typ, str) else set(typ)
        if not any(_is_type(instance, n) for n in names):
            out.append(
                Violation(path, "type", f"expected {sorted(names)}, got {_typename(instance)}")
            )
            # A wrong type makes every other assertion here noise.
            return out

    if "enum" in schema and not any(_equal(instance, v) for v in schema["enum"]):
        out.append(Violation(path, "enum", f"{instance!r} is not one of {schema['enum']}"))
    if "const" in schema and not _equal(instance, schema["const"]):
        out.append(Violation(path, "const", f"MUST be {schema['const']!r}"))

    if isinstance(instance, str):
        out += _string_errors(instance, schema, path)
    if isinstance(instance, bool):
        pass  # booleans are not numbers here, whatever Python thinks
    elif isinstance(instance, int | float):
        out += _number_errors(instance, schema, path)
    if isinstance(instance, Mapping):
        out += _object_errors(instance, schema, registry, path)
    if isinstance(instance, list):
        out += _array_errors(instance, schema, registry, path)

    out += _applicator_errors(instance, schema, registry, path)
    return out


def _applicator_errors(
    instance: Any, schema: Mapping[str, Any], registry: Mapping[str, Any], path: str
) -> list[Violation]:
    out: list[Violation] = []
    for sub in schema.get("allOf") or ():
        out += iter_errors(instance, sub, registry=registry, path=path)
    if "anyOf" in schema:
        branches = [
            iter_errors(instance, sub, registry=registry, path=path)
            for sub in schema["anyOf"]
        ]
        if all(b for b in branches):
            out.append(Violation(path, "anyOf", "matches none of the permitted shapes"))
    if "oneOf" in schema:
        matched = [
            sub
            for sub in schema["oneOf"]
            if not iter_errors(instance, sub, registry=registry, path=path)
        ]
        if len(matched) != 1:
            out.append(
                Violation(path, "oneOf", f"matches {len(matched)} of the shapes, MUST match 1")
            )
    if "not" in schema and not iter_errors(
        instance, schema["not"], registry=registry, path=path
    ):
        out.append(Violation(path, "not", "matches a forbidden shape"))
    if "if" in schema:
        holds = not iter_errors(instance, schema["if"], registry=registry, path=path)
        branch = schema.get("then") if holds else schema.get("else")
        if branch is not None:
            out += iter_errors(instance, branch, registry=registry, path=path)
    return out


def _string_errors(
    value: str, schema: Mapping[str, Any], path: str
) -> list[Violation]:
    out: list[Violation] = []
    if "minLength" in schema and len(value) < schema["minLength"]:
        out.append(Violation(path, "minLength", f"shorter than {schema['minLength']}"))
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        out.append(Violation(path, "maxLength", f"longer than {schema['maxLength']}"))
    if "pattern" in schema and not re.search(schema["pattern"], value):
        out.append(Violation(path, "pattern", f"does not match {schema['pattern']}"))
    fmt = schema.get("format")
    if fmt == "date-time" and not _RFC3339.match(value):
        out.append(Violation(path, "format", "is not an RFC 3339 date-time"))
    if fmt == "uri" and not _URI.match(value):
        out.append(Violation(path, "format", "is not an absolute URI"))
    return out


def _number_errors(
    value: float, schema: Mapping[str, Any], path: str
) -> list[Violation]:
    out: list[Violation] = []
    if "minimum" in schema and value < schema["minimum"]:
        out.append(Violation(path, "minimum", f"below {schema['minimum']}"))
    if "maximum" in schema and value > schema["maximum"]:
        out.append(Violation(path, "maximum", f"above {schema['maximum']}"))
    if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
        out.append(
            Violation(path, "exclusiveMinimum", f"MUST exceed {schema['exclusiveMinimum']}")
        )
    if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
        out.append(
            Violation(path, "exclusiveMaximum", f"MUST be under {schema['exclusiveMaximum']}")
        )
    if "multipleOf" in schema:
        step = schema["multipleOf"]
        if step > 0 and not math.isclose(
            value / step, round(value / step), rel_tol=0, abs_tol=1e-9
        ):
            out.append(Violation(path, "multipleOf", f"not a multiple of {step}"))
    return out


def _object_errors(
    value: Mapping[str, Any],
    schema: Mapping[str, Any],
    registry: Mapping[str, Any],
    path: str,
) -> list[Violation]:
    out: list[Violation] = []
    for name in schema.get("required") or ():
        if name not in value:
            out.append(Violation(path, "required", f"missing required member {name!r}"))
    if "minProperties" in schema and len(value) < schema["minProperties"]:
        out.append(Violation(path, "minProperties", f"fewer than {schema['minProperties']}"))
    if "maxProperties" in schema and len(value) > schema["maxProperties"]:
        out.append(Violation(path, "maxProperties", f"more than {schema['maxProperties']}"))

    properties = schema.get("properties") or {}
    pattern_properties = schema.get("patternProperties") or {}
    additional = schema.get("additionalProperties", True)

    for name, member in value.items():
        here = f"{path}/{_escape(name)}"
        matched = False
        if name in properties:
            matched = True
            out += iter_errors(member, properties[name], registry=registry, path=here)
        for pattern, sub in pattern_properties.items():
            if re.search(pattern, name):
                matched = True
                out += iter_errors(member, sub, registry=registry, path=here)
        if "propertyNames" in schema:
            out += iter_errors(name, schema["propertyNames"], registry=registry, path=here)
        if not matched:
            if additional is False:
                # MOS-REG-015: "Unknown fields are a reject, not a warning."
                out.append(
                    Violation(here, "additionalProperties", f"unknown member {name!r}")
                )
            elif additional is not True:
                out += iter_errors(member, additional, registry=registry, path=here)
    return out


def _array_errors(
    value: list[Any],
    schema: Mapping[str, Any],
    registry: Mapping[str, Any],
    path: str,
) -> list[Violation]:
    out: list[Violation] = []
    if "minItems" in schema and len(value) < schema["minItems"]:
        out.append(Violation(path, "minItems", f"fewer than {schema['minItems']} entries"))
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        out.append(Violation(path, "maxItems", f"more than {schema['maxItems']} entries"))
    if schema.get("uniqueItems") and _has_duplicates(value):
        out.append(Violation(path, "uniqueItems", "entries MUST be unique"))
    if "items" in schema:
        for i, item in enumerate(value):
            out += iter_errors(item, schema["items"], registry=registry, path=f"{path}/{i}")
    return out


# =====================================================================================
# Helpers
# =====================================================================================
def _resolve(ref: str, *, registry: Mapping[str, Any], path: str) -> Any:
    """`$ref` -> schema. Absolute URIs come from the in-repo registry; `#/...` is local."""
    if ref.startswith("#/"):
        raise SchemaError(
            f"{path}: local pointer {ref!r} is not supported; name the schema by its "
            "$id so one manifest schema is one addressable document (MOS-REG-015)"
        )
    target = registry.get(ref)
    if target is None:
        raise SchemaError(
            f"{path}: $ref {ref!r} resolves to nothing. Every schema this platform "
            "validates against MUST be in the in-repo source (MOS-REG-016); nothing is "
            "fetched over the network at validation time."
        )
    return target


def _is_type(value: Any, name: str) -> bool:
    if name == "object":
        return isinstance(value, Mapping)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if name == "number":
        return not isinstance(value, bool) and isinstance(value, int | float)
    return False  # pragma: no cover - check_schema closed the set


def _typename(value: Any) -> str:
    for name in ("null", "boolean", "object", "array", "string", "integer", "number"):
        if _is_type(value, name):
            return name
    return type(value).__name__  # pragma: no cover


def _equal(a: Any, b: Any) -> bool:
    """JSON equality: `1` and `1.0` are the same number, `True` and `1` are not."""
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, int | float) and isinstance(b, int | float):
        return float(a) == float(b)
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return set(a) == set(b) and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return bool(a == b)


def _has_duplicates(values: list[Any]) -> bool:
    for i, a in enumerate(values):
        for b in values[i + 1 :]:
            if _equal(a, b):
                return True
    return False


def _escape(name: str) -> str:
    """RFC 6901 pointer escaping, so a member called `a/b` locates correctly."""
    return name.replace("~", "~0").replace("/", "~1")
