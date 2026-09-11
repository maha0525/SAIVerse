"""Built-in default model constants.

When rotating to a new model version (e.g. Gemini preview expiry),
update BUILTIN_DEFAULT_LITE_MODEL here. All fallback references
across the codebase import from this single file.

This file also owns the model role table (role -> global env var and display
label) and the pure check that turns configured model names whose definition
cannot be found into screen warnings (``missing_model_warnings``). The check has
no DB access; the manager feeds it the current settings every time the screen
asks (``current_model_setting_warnings`` in manager/initialization.py).
"""
import logging
from typing import Callable, Dict, Iterable, List, Optional, Tuple

LOGGER = logging.getLogger(__name__)

# The single source of truth for the built-in fallback lite model.
# Used as the default for: DEFAULT_MODEL, LIGHTWEIGHT_MODEL, MEMORY_WEAVE_MODEL,
# ROUTER_MODEL, IMAGE_SUMMARY_MODEL, AGENTIC_MODEL, emotion module, etc.
BUILTIN_DEFAULT_LITE_MODEL = "gemini-3.1-flash-lite-preview"


# --- Model roles -------------------------------------------------------------

#: 役割 → 全体設定の環境変数名。チュートリアルのプリセット適用
#: (api/routes/tutorial.py) と、モデル設定の警告 (missing_model_warnings) が共有する。
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
#: - default_model: 設定キーの完全一致。ペルソナの値は manager/persona.py の
#:   _load_single_persona、全体設定の値は manager/initialization.py の
#:   _init_model_config が get_context_length / get_model_provider で引き、
#:   引けなければ代わりのモデルで読み込む。
#: - lightweight_model: 設定キーの完全一致。persona/core.py の
#:   lightweight_llm_client と sea/runtime.py の select_llm_client が
#:   get_context_length / get_model_provider で引く。
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

#: 定義が見つからないとき、組み込みの既定モデルへ切り替えて続ける役割
#: (saiverse/media_summary.py の _resolve_client_for_model)。
_MEDIA_SUMMARY_ROLES = frozenset({
    "image_summary_model",
    "audio_summary_model",
    "video_summary_model",
})


def _media_summary_fallback_is_defined() -> bool:
    """要約の代わりに使う組み込みの既定モデルの定義が、要約側と同じ引き方で引けるか。"""
    try:
        return _defined_by_find_model_config(BUILTIN_DEFAULT_LITE_MODEL)
    except Exception:
        LOGGER.warning(
            "Model config check failed for the media summary fallback %r; "
            "omitting the substitute sentence.",
            BUILTIN_DEFAULT_LITE_MODEL, exc_info=True,
        )
        return False


def role_model_is_defined(role: str, value: str) -> bool:
    """役割の値に定義があるかを、その値を実際に使う側と同じ引き方で返す。

    モデル設定の警告 (missing_model_warnings) と、グローバル設定の保存で標準モデルを
    動いているペルソナへ反映するか (api/routes/admin.py の update_env_vars) が
    同じ判定を使うためにある。判定が割れると「反映しなかったのに警告も出ない」になる。
    """
    return _ROLE_LOOKUPS[role](value)


def missing_model_warnings(
    entries: Iterable[Tuple[str, Optional[str]]],
    *,
    persona_id: Optional[str] = None,
    default_model_substitute: Optional[str] = None,
) -> List[Dict[str, str]]:
    """設定されたモデル名のうち、定義が見つからないものを画面の警告にして返す。

    Args:
        entries: ``(役割, 設定値)`` の並び。役割は ``_ROLE_LOOKUPS`` のキー。
            設定値が None / 空文字の役割は未設定として検査しない。
        persona_id: 渡すとペルソナ単位の文面、省略するとグローバル設定単位の文面になる。
        default_model_substitute: 標準モデルの定義が見つからないとき、代わりに
            動いているモデル。分かっているときだけ渡す。渡されていて、しかも
            設定値と違うときだけ、標準モデルの文面に「いまはモデル '…' で代わりに
            動いています。」を足す (設定値と同じなら代わりに動いているとは言えない)。

    一つの役割の検査が例外を出しても、ログに残して残りの役割の検査を続ける。
    """
    warnings: List[Dict[str, str]] = []
    for role, value in entries:
        if not value:
            continue
        try:
            if _ROLE_LOOKUPS[role](value):
                continue
        except Exception:
            LOGGER.warning(
                "Model config check failed (role=%s value=%r persona=%s); skipping.",
                role, value, persona_id, exc_info=True,
            )
            continue

        # 画面の名前に合わせる: ペルソナは「ペルソナ設定」、全体の値は
        # 「グローバル設定」の「モデルロール」タブで選ぶ。
        label = MODEL_ROLE_DESCRIPTIONS[role]["label"]
        if persona_id is not None:
            message = f"ペルソナ '{persona_id}' の{label} '{value}' の設定ファイルが見つかりません。"
            reselect = "ペルソナ設定から選び直してください。"
        else:
            message = f"グローバル設定の{label} '{value}' の設定ファイルが見つかりません。"
            reselect = "グローバル設定の「モデルロール」から選び直してください。"
        # 標準モデルは、読み込むときに代わりのモデルへ差し替わる
        # (manager/persona.py の _load_single_persona と
        # manager/initialization.py の _init_model_config)。
        if (
            role == "default_model"
            and default_model_substitute
            and default_model_substitute != value
        ):
            message += f"いまはモデル '{default_model_substitute}' で代わりに動いています。"
        # 画像・音声・動画要約は、グローバル設定の値の定義が引けないと組み込みの
        # 既定モデルへ切り替えて要約を続ける (saiverse/media_summary.py の
        # _resolve_client_for_model)。その既定モデルの定義も引けないときは要約
        # しないので、この一文は付けない。
        if (
            persona_id is None
            and role in _MEDIA_SUMMARY_ROLES
            and _media_summary_fallback_is_defined()
        ):
            message += (
                f"いまは組み込みの既定モデル '{BUILTIN_DEFAULT_LITE_MODEL}' に"
                "切り替えて要約を続けようとしています。"
            )
        message += reselect
        # 帰結を書くのは、コードで確かめられた役割だけ。Memory Weave は代わりの
        # モデルが無く、resolve_memory_weave_config が LookupError を出し続ける。
        # グローバル設定の値が効くのは、自分の Memory Weave モデルを持たない
        # ペルソナだけ (saiverse/memory_weave_llm.py の解決順)。
        if role == "memory_weave_model":
            if persona_id is not None:
                message += "選び直すまで、このペルソナの記憶の整理は止まったままになります。"
            else:
                message += (
                    "Memory Weaveモデルを個別に設定していないペルソナは、"
                    "選び直すまで記憶の整理が止まったままになります。"
                )

        LOGGER.warning(
            "Model config not found (role=%s value=%r persona=%s).",
            role, value, persona_id,
        )
        warnings.append({"source": "model_config", "message": message})
    return warnings
