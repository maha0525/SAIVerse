"""Built-in default model constants.

When rotating to a new model version (e.g. Gemini preview expiry),
update BUILTIN_DEFAULT_LITE_MODEL here. All fallback references
across the codebase import from this single file.

This file also owns the model role table (role -> global env var and display
label) and the pure check that turns configured model names whose definition
cannot be found into screen warnings (``missing_model_warnings``). The check has
no DB access; the manager feeds it the current settings every time the screen
asks (``current_model_setting_warnings`` in manager/initialization.py).

SAIVerse does not substitute another model when a configured one has no
definition (docs/intent/persona_model_selection.md, decision 7): the work that
needs that model stops until it is reselected, and the warnings say so.
"""
import logging
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

LOGGER = logging.getLogger(__name__)

# The single source of truth for the built-in default model.
# Used when the corresponding setting is empty: DEFAULT_MODEL, LIGHTWEIGHT_MODEL,
# MEMORY_WEAVE_MODEL, ROUTER_MODEL, IMAGE_SUMMARY_MODEL, AGENTIC_MODEL, emotion module, etc.
BUILTIN_DEFAULT_LITE_MODEL = "gemini-3.1-flash-lite-preview"


# --- Model roles -------------------------------------------------------------

#: 役割 → 全体設定の環境変数名。チュートリアルのプリセット適用
#: (api/routes/tutorial.py)、設定ファイルの無いモデル名を保存しない検査
#: (saiverse/persona_model_selection.py)、モデル設定の警告 (missing_model_warnings) が共有する。
MODEL_ROLES: Dict[str, str] = {
    "default_model": "SAIVERSE_DEFAULT_MODEL",
    "lightweight_model": "SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL",
    "memory_weave_model": "MEMORY_WEAVE_MODEL",
    "image_summary_model": "SAIVERSE_IMAGE_SUMMARY_MODEL",
    "audio_summary_model": "SAIVERSE_AUDIO_SUMMARY_MODEL",
    "video_summary_model": "SAIVERSE_VIDEO_SUMMARY_MODEL",
}

#: 役割の表示ラベルと説明。全体設定のモデルロール画面に出る文言で、ラベルは
#: ペルソナ設定画面 (frontend/src/components/SettingsModal.tsx) の欄名とも揃っている。
MODEL_ROLE_DESCRIPTIONS: Dict[str, Dict[str, str]] = {
    "default_model": {
        "label": "標準モデル",
        "description": "会話や複雑な推論に使用するメインモデル",
    },
    "lightweight_model": {
        "label": "軽量モデル",
        "description": "ルーティングやツール判断に使用する高速・安価なモデル",
    },
    "memory_weave_model": {
        "label": "Memory Weaveモデル",
        "description": "クロニクル・メモペディアの生成に使用するモデル",
    },
    "image_summary_model": {
        "label": "画像要約モデル",
        "description": "画像・ドキュメント要約生成用モデル（Vision対応モデル推奨）",
    },
    "audio_summary_model": {
        "label": "音声要約モデル",
        "description": "ユーザー添付音声の要約生成用モデル（Gemini系のみ対応）",
    },
    "video_summary_model": {
        "label": "動画要約モデル",
        "description": "ユーザー添付動画の要約生成用モデル（Gemini系のみ対応）",
    },
}


def _defined_by_config_key(value: str) -> bool:
    """設定キー (定義ファイル名) の完全一致だけで引く。"""
    from saiverse.model_configs import get_model_provider

    try:
        get_model_provider(value)
    except ValueError:
        return False
    return True


def _defined_by_find_model_config(value: str) -> bool:
    """find_model_config (設定キー / API モデル名 / ファイル名 / 接尾辞) で引く。"""
    from saiverse.model_configs import find_model_config

    _config_key, config = find_model_config(value)
    return bool(config)


#: 役割ごとの「定義があるか」の引き方。その値を実際に使う側と
#: 同じ引き方にする — 違う引き方だと「動いているのに警告が出る」「動いていない
#: のに出ない」になる。
#:
#: - default_model: 設定キーの完全一致。話す標準モデルを決める
#:   saiverse/persona_model_selection.py の resolve_speaking_model が引き、
#:   引けなければ代わりのモデルへ進まず「使えない」と決める。
#: - lightweight_model: 設定キーの完全一致。persona/core.py の
#:   lightweight_llm_client と saiverse/persona_model_selection.py の
#:   ReplyModelBinding が get_context_length / get_model_provider で引く。
#: - memory_weave_model: find_model_config。saiverse/memory_weave_llm.py の
#:   resolve_memory_weave_config が引く。
#: - image/audio/video_summary_model: find_model_config。全体設定の値を
#:   saiverse/media_summary.py が引く。ペルソナ単位の VISION_MODEL / AUDIO_MODEL /
#:   VIDEO_MODEL は読む箇所が無い (保存されるだけ) ので、ペルソナ単位の検査
#:   (manager/initialization.py の current_model_setting_warnings) には入れていない。
_ROLE_LOOKUPS: Dict[str, Callable[[str], bool]] = {
    "default_model": _defined_by_config_key,
    "lightweight_model": _defined_by_config_key,
    "memory_weave_model": _defined_by_find_model_config,
    "image_summary_model": _defined_by_find_model_config,
    "audio_summary_model": _defined_by_find_model_config,
    "video_summary_model": _defined_by_find_model_config,
}

_PERSONA_RESELECT = "ペルソナ設定で選び直すと"
_GLOBAL_RESELECT = "グローバル設定の「モデルロール」で選び直すと"


def role_model_is_defined(role: str, value: str) -> bool:
    """役割の値に定義があるかを、その値を実際に使う側と同じ引き方で返す。

    話す標準モデルの決め方 (saiverse/persona_model_selection.py)、設定ファイルの無い
    名前を保存しない検査、「SAIVerse にありません」の警告 (missing_model_warnings) が
    同じ判定を使うためにある。判定が割れると、保存を断ったのに警告が出ない、あるいは
    話せているのに止まっていると言う。
    """
    return _ROLE_LOOKUPS[role](value)


def _names(names: Optional[Sequence[str]]) -> str:
    return f" ({'、'.join(names)})" if names else ""


def _persona_message(role: str, name: str, value: str, override_model: Optional[str]) -> str:
    label = MODEL_ROLE_DESCRIPTIONS[role]["label"]
    if role == "default_model":
        if override_model:
            return (
                f"{name}の標準モデル '{value}' は SAIVerse にありません。"
                f"いまはチャット画面のモデル一時上書き '{override_model}' で話していますが、"
                f"上書きを解除すると{name}は止まります。"
                f"{_PERSONA_RESELECT}、上書きを解除しても再起動せずに話し続けられます。"
            )
        return (
            f"{name}の標準モデル '{value}' は SAIVerse にないため、{name}は止まっています。"
            f"{_PERSONA_RESELECT}、再起動しなくても話せるようになります。"
        )
    if role == "lightweight_model":
        return (
            f"{name}の軽量モデル '{value}' は SAIVerse にないため、{name}は軽量モデルを使う作業"
            "（返事の途中の作業や、自分から動く判断）ができず、止まっています。"
            f"{_PERSONA_RESELECT}、再起動しなくても続けられるようになります。"
        )
    if role == "memory_weave_model":
        return (
            f"{name}の{label} '{value}' は SAIVerse にないため、{name}の記憶の整理は止まっています。"
            f"{_PERSONA_RESELECT}、再起動しなくても整理が再開します。"
        )
    return (
        f"{name}の{label} '{value}' は SAIVerse にないため、このモデルを使う仕事は止まっています。"
        f"{_PERSONA_RESELECT}、再起動しなくても再開します。"
    )


def _global_message(
    role: str,
    value: str,
    override_model: Optional[str],
    names: Optional[Sequence[str]],
) -> str:
    label = MODEL_ROLE_DESCRIPTIONS[role]["label"]
    if role == "default_model":
        if override_model:
            return (
                f"グローバル設定の標準モデル '{value}' は SAIVerse にありません。"
                f"いまはチャット画面のモデル一時上書き '{override_model}' で話していますが、"
                f"上書きを解除すると、個別の標準モデルを持たないペルソナ{_names(names)}は止まります。"
                f"{_GLOBAL_RESELECT}、上書きを解除しても再起動せずに話し続けられます。"
            )
        if names:
            return (
                f"グローバル設定の標準モデル '{value}' は SAIVerse にないため、"
                f"個別の標準モデルを持たないペルソナ{_names(names)}は止まっています。"
                f"{_GLOBAL_RESELECT}、再起動しなくても話せるようになります。"
            )
        return (
            f"グローバル設定の標準モデル '{value}' は SAIVerse にありません。"
            "個別の標準モデルを持たないペルソナは、選び直すまで止まります。"
            f"{_GLOBAL_RESELECT}、再起動しなくても話せるようになります。"
        )
    if role == "lightweight_model":
        return (
            f"グローバル設定の軽量モデル '{value}' は SAIVerse にないため、"
            f"個別の軽量モデルを持たないペルソナ{_names(names)}は軽量モデルを使う作業"
            "（返事の途中の作業や、自分から動く判断）ができず、止まっています。"
            f"{_GLOBAL_RESELECT}、再起動しなくても続けられるようになります。"
        )
    if role == "memory_weave_model":
        return (
            f"グローバル設定の{label} '{value}' は SAIVerse にないため、"
            f"{label}を個別に設定していないペルソナ{_names(names)}の記憶の整理は止まっています。"
            f"{_GLOBAL_RESELECT}、再起動しなくても整理が再開します。"
        )
    # 画像・音声・動画の要約は、代わりのモデルで要約しない (saiverse/media_summary.py)。
    return (
        f"グローバル設定の{label} '{value}' は SAIVerse にないため、要約は止まっています。"
        f"{_GLOBAL_RESELECT}、再起動しなくても要約されるようになります。"
    )


def missing_model_warnings(
    entries: Iterable[Tuple[str, Optional[str]]],
    *,
    persona_name: Optional[str] = None,
    override_model: Optional[str] = None,
    affected_persona_names: Optional[Mapping[str, Sequence[str]]] = None,
) -> List[Dict[str, str]]:
    """設定されたモデル名のうち、定義が見つからないものを画面の警告にして返す。

    Args:
        entries: ``(役割, 設定値)`` の並び。役割は ``_ROLE_LOOKUPS`` のキー。
            設定値が None / 空文字の役割は未設定として検査しない。
        persona_name: 渡すとペルソナ単位の文面 (その名前で呼ぶ)、省略すると
            グローバル設定単位の文面になる。
        override_model: チャット画面のモデル一時上書きが有効なら、そのモデル名。
            標準モデルの文面を「止まっています」ではなく「上書きを解除すると止まる」にする。
        affected_persona_names: グローバル設定単位の文面で、役割ごとに「その値を
            使っているペルソナ (個別の値を持たないペルソナ)」の名前。分からないときは省略する。

    一つの役割の検査が例外を出しても、ログに残して残りの役割の検査を続ける。
    """
    warnings: List[Dict[str, str]] = []
    for role, value in entries:
        if not value or not str(value).strip():
            continue
        try:
            if _ROLE_LOOKUPS[role](value):
                continue
        except Exception:
            LOGGER.warning(
                "Model config check failed (role=%s value=%r persona=%s); skipping.",
                role, value, persona_name, exc_info=True,
            )
            continue

        if persona_name is not None:
            message = _persona_message(role, persona_name, value, override_model)
        else:
            names = (affected_persona_names or {}).get(role)
            message = _global_message(role, value, override_model, names)

        LOGGER.warning(
            "Model config not found (role=%s value=%r persona=%s).",
            role, value, persona_name,
        )
        warnings.append({"source": "model_config", "message": message})
    return warnings
