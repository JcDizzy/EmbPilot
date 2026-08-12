"""
Schema-driven CLI flags for ``embpilot tool``.

Every tool's JSON Schema (from ``build_tool_definitions``) is translated into
argparse flags so humans and agents can avoid fragile inline JSON quoting:

    embpilot tool connect_serial --port COM3 --baudrate 115200 --line-ending crlf

``--json`` remains the canonical form; flags supplied explicitly override the
matching keys of ``--json`` (documented precedence).
"""

from __future__ import annotations

import argparse

from embpilot.mcp_compat import tool_input_schema
from embpilot.mcp_contracts import build_tool_definitions


def schema_properties(tool_name: str) -> dict[str, dict]:
    """Return the tool's schema properties, or {} for an unknown tool."""
    tool = next(
        (item for item in build_tool_definitions() if item.name == tool_name),
        None,
    )
    if tool is None:
        return {}
    schema = tool_input_schema(tool)
    return schema.get("properties") or {}


def _flag_dest(prop_name: str) -> str:
    """Namespace key for a schema flag, isolated from subcommand dests."""
    return f"schema_{prop_name}"


def add_schema_flags(parser: argparse.ArgumentParser, tool_name: str) -> None:
    """Add one ``--<prop>`` flag (kebab-case) per tool schema property."""
    for prop_name, prop in schema_properties(tool_name).items():
        flag = "--" + prop_name.replace("_", "-")
        dest = _flag_dest(prop_name)
        if prop.get("type") == "boolean":
            parser.add_argument(
                flag,
                dest=dest,
                action="store_true",
                default=None,
                help=prop.get("description", ""),
            )
            parser.add_argument(
                "--no-" + flag[2:],
                dest=dest,
                action="store_false",
                default=None,
                help=f"explicitly disable {prop_name}",
            )
            continue
        kwargs: dict = {}
        if prop.get("enum"):
            kwargs["choices"] = prop["enum"]
        if prop.get("type") == "integer":
            kwargs["type"] = int
        elif prop.get("type") == "number":
            kwargs["type"] = float
        parser.add_argument(
            flag,
            dest=dest,
            default=None,
            help=prop.get("description", ""),
            **kwargs,
        )


def collect_flag_arguments(
    args: argparse.Namespace,
    tool_name: str,
    base: dict,
) -> dict:
    """Merge ``--json`` base arguments with explicitly supplied flags.

    Flags override the matching ``--json`` keys; unspecified flags are
    ignored (argparse defaults are None).
    """
    merged = dict(base)
    for prop_name in schema_properties(tool_name):
        value = getattr(args, _flag_dest(prop_name), None)
        if value is not None:
            merged[prop_name] = value
    return merged
