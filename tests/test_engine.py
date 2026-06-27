"""
Tests for the FrameAssembler, RingBuffer, and consumer pipeline.
"""

from __future__ import annotations

# TODO(phase-2): add unit tests for:
#   - FrameAssembler.feed with various chunk sizes
#   - FrameAssembler timeout flush
#   - RingBuffer overflow / snapshot
#   - LogProducer integration with a mock StreamReader
