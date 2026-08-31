"""Memory weave 系ジョブ (Chronicle / Track Chronicle / 編纂) の LLM 解決。

解決チェーンはここの 1 本に固定する:

    persona.memory_weave_model → env MEMORY_WEAVE_MODEL → BUILTIN_DEFAULT_LITE_MODEL

背景 (2026-07-18): 編纂 (sai_memory/curation_ops.py) が独自解決を持ち、
存在しない ``persona.LIGHTWEIGHT_MODEL`` (実属性は小文字 ``lightweight_model``)
を getattr していたため、ペルソナ設定が常に素通りしてグローバル既定の
軽量モデルへ貫通していた。weave 級の記憶仕事のモデル解決は必ずここを通し、
呼び出し側に解決ロジックを複製しないこと。

再発防止の設計:

- ペルソナ側の属性が **存在しない** 場合 (属性名の変更・不完全なスタブ) は
  黙って fallback せず WARNING で対象の型名ごと報告する。
- 解決結果は出所 (persona / env / builtin) 付きで必ず INFO ログに残す —
  「どのモデルで走ったか」がログから常に再構成できるようにする。
"""
import logging
import os
from typing import Any, Tuple

LOGGER = logging.getLogger(__name__)

#: 解決結果の出所ラベル
SOURCE_PERSONA = "persona"
SOURCE_ENV = "env"
SOURCE_BUILTIN = "builtin"


def resolve_memory_weave_model(persona: Any) -> Tuple[str, str]:
    """weave 用モデル名を解決して ``(model_name, source)`` を返す。

    ``persona`` が ``memory_weave_model`` 属性を持たない場合 (スタブ・
    属性名の変更) は WARNING を出してグローバル設定へ進む — 黙って
    None 扱いにしない。
    """
    model_name = None
    if persona is None:
        LOGGER.warning(
            "[weave_model] persona is None; falling back to global settings"
        )
    elif not hasattr(persona, "memory_weave_model"):
        LOGGER.warning(
            "[weave_model] %s has no attribute 'memory_weave_model' "
            "(attribute renamed? incomplete stub?); falling back to global settings",
            type(persona).__name__,
        )
    else:
        model_name = persona.memory_weave_model
    if model_name:
        return str(model_name), SOURCE_PERSONA

    env_model = os.getenv("MEMORY_WEAVE_MODEL")
    if env_model:
        return env_model, SOURCE_ENV

    from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
    return BUILTIN_DEFAULT_LITE_MODEL, SOURCE_BUILTIN


def resolve_memory_weave_config(
    persona: Any, *, purpose: str
) -> Tuple[str, dict, str]:
    """weave 用モデルを解決して ``(model_id, model_config, source)`` を返す。

    表示名など model_config を自分で読みたい呼び出し元向け。クライアントまで
    要るだけなら :func:`get_memory_weave_client` を使う。

    Raises:
        LookupError: 解決したモデル名の設定が見つからない場合。
    """
    from saiverse.model_configs import find_model_config

    model_name, source = resolve_memory_weave_model(persona)
    model_id, model_config = find_model_config(model_name)
    if not model_config:
        raise LookupError(
            f"memory weave model not found: {model_name!r} (source={source})"
        )
    LOGGER.info(
        "[weave_model] purpose=%s persona=%s model=%s (source=%s)",
        purpose, getattr(persona, "persona_id", None), model_id, source,
    )
    return model_id, model_config, source


def build_memory_weave_client(model_id: str, model_config: dict) -> Any:
    """:func:`resolve_memory_weave_config` の結果から LLM クライアントを構築する。"""
    from llm_clients.factory import get_llm_client

    provider = model_config.get("provider")
    context_length = model_config.get("context_length", 128000)
    return get_llm_client(model_id, provider, context_length, config=model_config)


def get_memory_weave_client(persona: Any, *, purpose: str) -> Any:
    """weave 用の LLM クライアントを解決チェーンから構築して返す。

    Args:
        persona: PersonaCore (または互換スタブ)。
        purpose: ログ用の用途ラベル (例: "chronicle" / "track_chronicle" /
            "curation")。

    Raises:
        LookupError: 解決したモデル名の設定が見つからない場合。
    """
    model_id, model_config, _source = resolve_memory_weave_config(
        persona, purpose=purpose
    )
    return build_memory_weave_client(model_id, model_config)
