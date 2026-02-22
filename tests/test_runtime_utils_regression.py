from sea import runtime
from sea import runtime_llm
from sea import runtime_utils


def test_runtime_exposes_shared_format_helper() -> None:
    assert runtime._format is runtime_utils._format


def test_runtime_exposes_shared_streaming_helper() -> None:
    assert runtime._is_llm_streaming_enabled is runtime_utils._is_llm_streaming_enabled


def test_runtime_llm_exposes_shared_helpers() -> None:
    assert runtime_llm._format is runtime_utils._format
    assert runtime_llm._is_llm_streaming_enabled is runtime_utils._is_llm_streaming_enabled
