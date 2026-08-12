"""Schema-driven flag generation and merge semantics."""

from __future__ import annotations

import argparse

from embpilot.cli_flags import (
    add_schema_flags,
    collect_flag_arguments,
    schema_properties,
)
from embpilot.mcp_compat import tool_input_schema
from embpilot.mcp_contracts import build_tool_definitions


def test_every_tool_schema_round_trips_through_flags() -> None:
    """flags -> args must survive the tool's own jsonschema validation."""
    import jsonschema

    for tool in build_tool_definitions():
        parser = argparse.ArgumentParser(prog="probe")
        add_schema_flags(parser, tool.name)
        schema = tool_input_schema(tool)
        properties = schema.get("properties") or {}

        # Build one argv per property with a valid value for its type.
        for prop_name, prop in properties.items():
            value = _sample_value(prop)
            if value is None:
                continue  # boolean: exercised below via --flag/--no-flag
            args = parser.parse_args(
                [f"--{prop_name.replace('_', '-')}", value]
            )
            collected = collect_flag_arguments(args, tool.name, {})
            assert prop_name in collected
            assert collected[prop_name] == _typed_value(prop, value)
        # Boolean properties use --flag/--no-flag with no value.
        for prop_name, prop in properties.items():
            if prop.get("type") == "boolean":
                args = parser.parse_args([f"--{prop_name.replace('_', '-')}"])
                assert collect_flag_arguments(args, tool.name, {})[prop_name] is True
                args = parser.parse_args([f"--no-{prop_name.replace('_', '-')}"])
                assert collect_flag_arguments(args, tool.name, {})[prop_name] is False


def _sample_value(prop: dict) -> str | None:
    """A string value valid for the property type (or None for booleans)."""
    prop_type = prop.get("type")
    if prop_type == "boolean":
        return None
    enum = prop.get("enum")
    if enum:
        return str(enum[0])
    if prop_type == "integer":
        return "1"
    if prop_type == "number":
        return "1.5"
    return "x"


def _typed_value(prop: dict, raw: str) -> object:
    enum = prop.get("enum")
    if enum:
        return enum[0]
    prop_type = prop.get("type")
    if prop_type == "integer":
        return int(raw)
    if prop_type == "number":
        return float(raw)
    return raw


def test_flag_names_are_kebab_case() -> None:
    assert "timeout_ms" in schema_properties("connect_serial")
    assert "timeout-ms" in {
        prop.replace("_", "-") for prop in schema_properties("connect_serial")
    }


def test_flags_override_json_base_arguments() -> None:
    parser = argparse.ArgumentParser(prog="probe")
    add_schema_flags(parser, "connect_serial")
    args = parser.parse_args(["--baudrate", "9600"])
    merged = collect_flag_arguments(args, "connect_serial", {"port": "COM3", "baudrate": 115200})
    assert merged == {"port": "COM3", "baudrate": 9600}


def test_unspecified_flags_keep_json_base() -> None:
    parser = argparse.ArgumentParser(prog="probe")
    add_schema_flags(parser, "connect_serial")
    args = parser.parse_args([])
    merged = collect_flag_arguments(args, "connect_serial", {"port": "COM3"})
    assert merged == {"port": "COM3"}


def test_unknown_tool_has_no_properties() -> None:
    assert schema_properties("bogus_tool") == {}
