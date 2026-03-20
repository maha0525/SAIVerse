"""Smoke test for pytest configuration."""
import pytest


def test_pytest_is_configured():
    """Verify pytest is properly installed and configured."""
    assert True


def test_basic_math():
    """Basic assertion test."""
    assert 1 + 1 == 2