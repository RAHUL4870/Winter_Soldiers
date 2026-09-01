#!/bin/bash

set -e

echo "═════════════════════════════════════════════════════════════"
echo "T-034 Integration Test Verification"
echo "═════════════════════════════════════════════════════════════"

echo ""
echo "1. Running SSE position stream integration test (first run)…"
pytest tests/integration/test_sse_position_stream.py::test_sse_position_stream_comprehensive -v -s

echo ""
echo "2. Running determinism test…"
pytest tests/integration/test_sse_position_stream.py::test_sse_position_stream_determinism -v -s

echo ""
echo "3. Full integration test suite…"
pytest tests/integration/ -v

echo ""
echo "4. Lint and type-check…"
ruff check tests/integration/test_sse_position_stream.py
mypy tests/integration/test_sse_position_stream.py

echo ""
echo "═════════════════════════════════════════════════════════════"
echo "✓ ALL TESTS PASSED"
echo "═════════════════════════════════════════════════════════════"
