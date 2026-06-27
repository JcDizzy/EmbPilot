"""
Shared test fixtures and helpers.
"""
from __future__ import annotations

import asyncio
import pytest


@pytest.fixture
def event_loop():
    """Provide a clean event loop for each async test."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
