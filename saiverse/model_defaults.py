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

The same table also owns which destinations a role can actually talk to
(``role_model_save_rejection``). A model on a ``jev_compat`` provider answers
typed questions with probabilities only; the ordinary conversation clients
(``llm_clients/factory.py``) cannot speak to it at all. Assigning one to a
conversation role would stop the persona the moment it tried to reply, so those
saves are refused, and values that arrived through some other path (a hand-edited
.env) are reported as screen warnings. The same judgment is published as
``is_reflex_only_model`` so the model-list APIs can mark those models and the
conversation dropdowns can leave them out — being refused after picking one is a
worse screen than never seeing it.

The reverse direction is checked but never refused at save time: the reflex
judgment role takes either a ``jev_compat`` destination or an ordinary LLM (the
judgment layer turns the typed questions into a prompt for the latter), so an
ordinary model passes the check on its own. What the check still catches is a
value the judgment layer cannot resolve at all — a ``jev_compat`` model that
declares no destination, one that does not answer the question type the caller
asks, or a credential/destination pair the provider check refuses. Those reach
the screen as warnings while the save goes through.
See ``docs/intent/reflex_judgment.md`` §2.
"""
import logging
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

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
    # 反射判断 (docs/intent/reflex_judgment.md)。組み込みの既定は空 = 役割は
    # 未割り当てで、反射判断を使う機能はどれも動かない。黙って費用が発生する
    # 経路を作らないため、チュートリアルのプリセットもここには値を配らない。
    "reflex_judgment_model": "SAIVERSE_REFLEX_JUDGMENT_MODEL",
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
    "reflex_judgment_model": {
        "label": "反射判断",
        "description": (
            "自動想起の選別などの、型付きの質問に確率だけで答える判断に使うモデル。"
            "未設定なら反射判断は動きません"
        ),
    },
}


def _config_by_config_key(value: str) -> Optional[Mapping[str, Any]]:
    """設定キー (定義ファイル名) の完全一致だけで引く。"""
    from saiverse import model_configs

    return model_configs.MODEL_CONFIGS.get(value)


def _config_by_find_model_config(value: str) -> Optional[Mapping[str, Any]]:
    """find_model_config (設定キー / API モデル名 / ファイル名 / 接尾辞) で引く。"""
    from saiverse.model_configs import find_model_config

    _config_key, config = find_model_config(value)
    return config or None


#: 役割ごとの「その値のモデル設定をどう引くか」。その値を実際に使う側と
#: 同じ引き方にする — 違う引き方だと「動いているのに警告が出る」「動いていない
#: のに出ない」になる。
#:
#: 「定義があるか」(:func:`role_model_is_defined`) も「その役割の宛先として
#: 噛み合っているか」(``_ROLE_DESTINATION_CHECKS``) も、この一枚から引いた設定で
#: 判定する。引き方を二枚に分けると、保存を断った値と警告に出る値がずれる。
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
#: - reflex_judgment_model: 設定キーの完全一致。saiverse/reflex_judgment.py の
#:   resolve_backend が MODEL_CONFIGS を設定キーで引く。
_ROLE_CONFIG_LOOKUPS: Dict[str, Callable[[str], Optional[Mapping[str, Any]]]] = {
    "default_model": _config_by_config_key,
    "lightweight_model": _config_by_config_key,
    "memory_weave_model": _config_by_find_model_config,
    "image_summary_model": _config_by_find_model_config,
    "audio_summary_model": _config_by_find_model_config,
    "video_summary_model": _config_by_find_model_config,
    "reflex_judgment_model": _config_by_config_key,
}


def _protocol_of(config: Mapping[str, Any]) -> Optional[str]:
    """モデル設定が話す protocol (provider_ref から受け継いだものも含む)。

    ``MODEL_CONFIGS`` に載っている設定は読み込みの時点で provider_ref を解決済み
    なので ``protocol`` / ``provider`` を見れば足りる。``find_model_config`` は
    ``MODEL_CONFIGS`` に入らなかったファイルを直接読んで返すことがあり、その設定は
    provider_ref が未解決のままなので、そのときだけ provider を辿る。
    """
    protocol = config.get("protocol") or config.get("provider")
    if protocol:
        return str(protocol)
    provider_ref = config.get("provider_ref")
    if not provider_ref:
        return None
    from saiverse.provider_configs import get_provider

    provider = get_provider(str(provider_ref))
    if not isinstance(provider, Mapping):
        return None
    value = provider.get("protocol")
    return str(value) if value else None


def _reflex_destination_mismatch(_role: str, value: str) -> bool:
    """反射判断の役割に、反射判断の宛先として解決できない値が割り当たっているか。

    定義はあるので「SAIVerse にありません」の検査は素通りするが、その値では反射判断が
    動かないことがある。**第 2 段で通常の LLM も答える側になった**ので、ここに残るのは
    「どちらの答える側としても解決できない設定」だけ — jev 互換なのに宛先の URL が
    宣言されていない、答えられる質問の型が呼び出し側の投げる型を含まない、キーと宛先の
    組が照合に通らない、といった構造の不備 (docs/intent/reflex_judgment.md §2)。
    通常の LLM の割り当ては自然に通る (下記のとおり実行側と同じ関数を呼ぶので、変換層が
    入った時点でこの検査は自動的に緩んだ)。

    割り当てても実行時に WARNING が出て想起が従来方式へ戻るだけで、画面には何も出ない —
    それでは設定ミスが見えないので、ここで画面の警告に載せる。**保存は弾かない**
    (この役割は ``_SAVE_BLOCKED_ROLES`` に入れていない)。

    判定は実行側とまったく同じ関数 (saiverse/reflex_judgment.py の
    ``resolve_backend``) を呼んで行う。protocol だけを自前で見直すと、protocol は
    合っているのに宛先の URL が空・答えられる型が空といった不備が画面に出ない。
    同じ関数に寄せてあれば、将来 resolve_backend に検査が増えても画面が追従する。

    答えられる質問の型も ``FIRST_STAGE_QUESTION_TYPE`` (noul) まで含めて見る。いま
    この層に仕事を頼むのは自動想起の選別だけで、その仕事は noul しか投げない。noul に
    答えない宛先を画面が「噛み合っている」と言うと、自動想起は候補の集め方だけを広げた
    まま毎ターン判定に失敗し、その設定ミスはどこにも出ないままになる。

    キーの有無は判定に含めない (resolve_backend はキーを見ない)。キー欠落は構造の
    不備ではなく、実行時に WARNING で知らせる別の話。
    """
    from saiverse import model_configs
    from saiverse.reflex_judgment import (
        FIRST_STAGE_QUESTION_TYPE,
        ReflexJudgmentUnavailable,
        resolve_backend,
    )

    if not model_configs.MODEL_CONFIGS.get(value):
        # 定義が無い件は「SAIVerse にありません」の警告が拾う。
        return False
    try:
        resolve_backend(model_key=value, required_type=FIRST_STAGE_QUESTION_TYPE)
    except ReflexJudgmentUnavailable:
        return True
    return False


def is_reflex_only_model(value: str) -> bool:
    """その名前のモデルが反射判断専用の宛先 (jev 互換) かどうか。

    jev 互換の宛先は型付きの質問に確率で答えるだけの相手で、文章を書かせることが
    できない (docs/intent/reflex_judgment.md §6-4)。モデルの一覧を返す API
    (api/routes/info.py・config.py・tutorial.py) が各モデルにこの印を載せ、会話系の
    モデル選択欄が印の付いたものを選択肢に出さないために使う。一覧そのものからは
    落とさない — モデル管理画面と反射判断の選択欄には出し続ける必要がある。

    判定は保存の関所 (:func:`role_model_save_rejection` が通す
    ``_jev_only_destination``) と同じ一枚。だから「選べるのに保存だけ断られる」
    「選択肢に出ないのに保存は通る」のずれは生まれない。

    定義が引けない名前は False を返す (「SAIVerse にありません」は別の検査が拾う)。
    """
    from saiverse.reflex_judgment import JEV_COMPAT_PROTOCOL

    config = _config_by_find_model_config(value)
    if config is None:
        return False
    return _protocol_of(config) == JEV_COMPAT_PROTOCOL


def _jev_only_destination(role: str, value: str) -> bool:
    """反射判断以外の役割に、反射判断専用の宛先 (jev 互換) が割り当たっているか。

    jev 互換の宛先は型付きの質問に確率で答えるだけの相手で、文章を書かせることが
    できない。通常の会話クライアント (llm_clients/factory.py) はこの protocol を
    知らないので、割り当てられたペルソナは話そうとした時点で失敗する
    (docs/intent/reflex_judgment.md §6-4)。保存の時点で断り、env の直書きなどで
    既に入っている値は画面の警告で知らせる。

    宛先の種類そのものの判定は :func:`is_reflex_only_model` に持たせてある (画面が
    選択肢から外す判定と同じ一枚にするため)。ここが足すのは「その役割の引き方で
    定義が引けるか」だけ — 引けない値は False を返す (「SAIVerse にありません」の
    検査が拾う。同じ件で二つの警告を出さない)。
    """
    if _ROLE_CONFIG_LOOKUPS[role](value) is None:
        return False
    return is_reflex_only_model(value)


#: 「定義はあるが、その役割の宛先として噛み合っていない」を見る役割ごとの検査。
#: 引数は ``(役割, 値)``。
#:
#: 反射判断の役割は「その宛先で反射判断ができるか」(実行側と同じ関数を通す)、
#: それ以外の全役割は「反射判断専用の宛先が紛れ込んでいないか」。表を役割ごとに
#: 手で並べず ``MODEL_ROLES`` から組むのは、役割を増やしたときに検査の無い役割が
#: 黙って生まれないようにするため。
_ROLE_DESTINATION_CHECKS: Dict[str, Callable[[str, str], bool]] = {
    role: (
        _reflex_destination_mismatch
        if role == "reflex_judgment_model"
        else _jev_only_destination
    )
    for role in MODEL_ROLES
}

#: 保存を断る理由 (:func:`role_model_save_rejection` の返り値)。
SAVE_REJECT_UNDEFINED = "undefined"      # その名前の定義が SAIVerse に無い
SAVE_REJECT_DESTINATION = "destination"  # 定義はあるが、その役割では使えない宛先

#: 噛み合わない値を**保存の時点で**断る役割。反射判断の役割は入れない — 反射判断は
#: jev 互換の宛先も通常の LLM も答える側にできるので、宛先の種類を理由に断るものが
#: 無い。解決できない設定も保存は通し、画面の警告だけで知らせる
#: (docs/intent/reflex_judgment.md §2)。この非対称は仕様。
_SAVE_BLOCKED_ROLES = frozenset(MODEL_ROLES) - {"reflex_judgment_model"}

_PERSONA_RESELECT = "ペルソナ設定で選び直すと"
_GLOBAL_RESELECT = "グローバル設定の「モデルロール」で選び直すと"


def role_model_is_defined(role: str, value: str) -> bool:
    """役割の値に定義があるかを、その値を実際に使う側と同じ引き方で返す。

    話す標準モデルの決め方 (saiverse/persona_model_selection.py)、設定ファイルの無い
    名前を保存しない検査、「SAIVerse にありません」の警告 (missing_model_warnings) が
    同じ判定を使うためにある。判定が割れると、保存を断ったのに警告が出ない、あるいは
    話せているのに止まっていると言う。
    """
    return _ROLE_CONFIG_LOOKUPS[role](value) is not None


def role_model_save_rejection(role: str, value: str) -> Optional[str]:
    """その役割にその値を保存してよいかを調べ、断る理由を返す (保存してよければ None)。

    見るのは二つ — 定義がその名前で引けるか (:func:`role_model_is_defined`) と、
    その役割で使える宛先か (``_ROLE_DESTINATION_CHECKS``)。後者を保存で断るのは
    ``_SAVE_BLOCKED_ROLES`` の役割だけ。

    ペルソナ設定の保存・グローバル設定のモデルロールの保存・チャット画面のモデル
    一時上書きが、同じ判定をここから引く (入口ごとに書くと、ある画面からだけ
    会話の止まる設定を作れる穴が残る)。

    Raises:
        Exception: 定義の引き方そのものが失敗したとき。呼び出し側はこれを捕まえて
            「保存しない」に倒す (確かめられない名前を保存したあとで「無かった」と
            分かっても遅い)。
    """
    if not role_model_is_defined(role, value):
        return SAVE_REJECT_UNDEFINED
    check = _ROLE_DESTINATION_CHECKS.get(role)
    if role in _SAVE_BLOCKED_ROLES and check is not None and check(role, value):
        return SAVE_REJECT_DESTINATION
    return None


def _names(names: Optional[Sequence[str]]) -> str:
    return f" ({'、'.join(names)})" if names else ""


#: 反射判断の役割の文面で共通の「何ができなくなっているか」の一文。
_REFLEX_STOPPED = "型付きの質問に確率で答える判断 (自動想起の強化など) は動いていません。"


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
    if role == "reflex_judgment_model":
        return (
            f"グローバル設定の{label}のモデル '{value}' は SAIVerse にないため、"
            f"{label}のモデルを個別に設定していないペルソナ{_names(names)}の"
            f"{_REFLEX_STOPPED}"
            f"{_GLOBAL_RESELECT}、再起動しなくても動くようになります。"
        )
    # 画像・音声・動画の要約は、代わりのモデルで要約しない (saiverse/media_summary.py)。
    return (
        f"グローバル設定の{label} '{value}' は SAIVerse にないため、要約は止まっています。"
        f"{_GLOBAL_RESELECT}、再起動しなくても要約されるようになります。"
    )


#: 反射判断専用の宛先が会話の役割に入っているときの、理由の一文。
_JEV_ONLY = "は反射判断だけに使える宛先のため、"


def _jev_only_persona_message(role: str, name: str, value: str) -> str:
    """反射判断専用の宛先がペルソナの役割に入っているときの文面。

    いま止まっているとは言い切らず「この設定のままでは〜できません」と書く —
    チャット画面のモデル一時上書きが効いている間、そのペルソナは上書きのモデルで
    話せているので、「止まっています」は事実にならないことがある。
    """
    label = MODEL_ROLE_DESCRIPTIONS[role]["label"]
    if role == "default_model":
        return (
            f"{name}の標準モデル '{value}'{_JEV_ONLY}会話には使えません。"
            f"この設定のままでは{name}は話せません。"
            f"{_PERSONA_RESELECT}、再起動しなくても話せるようになります。"
        )
    if role == "lightweight_model":
        return (
            f"{name}の軽量モデル '{value}'{_JEV_ONLY}会話には使えません。"
            f"この設定のままでは{name}は軽量モデルを使う作業"
            "（返事の途中の作業や、自分から動く判断）ができません。"
            f"{_PERSONA_RESELECT}、再起動しなくても続けられるようになります。"
        )
    if role == "memory_weave_model":
        return (
            f"{name}の{label} '{value}'{_JEV_ONLY}会話には使えません。"
            f"この設定のままでは{name}の記憶の整理は動きません。"
            f"{_PERSONA_RESELECT}、再起動しなくても整理が再開します。"
        )
    return (
        f"{name}の{label} '{value}'{_JEV_ONLY}{label}には使えません。"
        "この設定のままでは、このモデルを使う仕事は動きません。"
        f"{_PERSONA_RESELECT}、再起動しなくても再開します。"
    )


def _jev_only_global_message(role: str, value: str, names: Optional[Sequence[str]]) -> str:
    """反射判断専用の宛先がグローバル設定の役割に入っているときの文面。"""
    label = MODEL_ROLE_DESCRIPTIONS[role]["label"]
    if role == "default_model":
        return (
            f"グローバル設定の標準モデル '{value}'{_JEV_ONLY}会話には使えません。"
            f"この設定のままでは、個別の標準モデルを持たないペルソナ{_names(names)}は話せません。"
            f"{_GLOBAL_RESELECT}、再起動しなくても話せるようになります。"
        )
    if role == "lightweight_model":
        return (
            f"グローバル設定の軽量モデル '{value}'{_JEV_ONLY}会話には使えません。"
            f"この設定のままでは、個別の軽量モデルを持たないペルソナ{_names(names)}は"
            "軽量モデルを使う作業（返事の途中の作業や、自分から動く判断）ができません。"
            f"{_GLOBAL_RESELECT}、再起動しなくても続けられるようになります。"
        )
    if role == "memory_weave_model":
        return (
            f"グローバル設定の{label} '{value}'{_JEV_ONLY}会話には使えません。"
            f"この設定のままでは、{label}を個別に設定していないペルソナ{_names(names)}の"
            "記憶の整理は動きません。"
            f"{_GLOBAL_RESELECT}、再起動しなくても整理が再開します。"
        )
    # 画像・音声・動画の要約。
    return (
        f"グローバル設定の{label} '{value}'{_JEV_ONLY}要約には使えません。"
        "この設定のままでは要約は動きません。"
        f"{_GLOBAL_RESELECT}、再起動しなくても要約されるようになります。"
    )


#: 反射判断の役割に、反射判断の宛先として解決できない値が入っているときの、理由の一文。
#: 通常の LLM は解決できるので、ここに来るのは設定そのものが壊れている値だけ。
#: 「SAIVerse にありません」の文面と同じく、モデル名の引用符との間に空白を置く
#: (``_JEV_ONLY`` は「'名前'は反射判断だけに〜」と続ける別の形)。
_REFLEX_UNRESOLVABLE = "は反射判断の宛先として解決できないため、"


def _reflex_mismatch_message(
    value: str, persona_name: Optional[str], names: Optional[Sequence[str]],
) -> str:
    """反射判断の役割に、反射判断の宛先として解決できない値が入っているときの文面。

    第 2 段で通常の LLM も答える側になったので、ここに来るのは「jev 互換なのに宛先が
    宣言されていない」「投げる型に答えない」「キーと宛先の組が照合に通らない」といった、
    どちらの答える側としても解決できない設定 (:func:`_reflex_destination_mismatch`)。
    """
    label = MODEL_ROLE_DESCRIPTIONS["reflex_judgment_model"]["label"]
    if persona_name is not None:
        return (
            f"{persona_name}の{label}のモデル '{value}' {_REFLEX_UNRESOLVABLE}"
            f"{persona_name}の{_REFLEX_STOPPED}"
            f"{_PERSONA_RESELECT}、再起動しなくても動くようになります。"
        )
    return (
        f"グローバル設定の{label}のモデル '{value}' {_REFLEX_UNRESOLVABLE}"
        f"{label}のモデルを個別に設定していないペルソナ{_names(names)}の"
        f"{_REFLEX_STOPPED}"
        f"{_GLOBAL_RESELECT}、再起動しなくても動くようになります。"
    )


def _mismatch_message(
    role: str,
    value: str,
    persona_name: Optional[str],
    names: Optional[Sequence[str]],
) -> str:
    """定義はあるのに、その役割の宛先として噛み合っていない値の文面。

    二方向ある — 反射判断の役割に、反射判断ができない宛先。会話や要約の役割に、
    反射判断専用の宛先 (``_ROLE_DESTINATION_CHECKS``)。
    """
    if role == "reflex_judgment_model":
        return _reflex_mismatch_message(value, persona_name, names)
    if persona_name is not None:
        return _jev_only_persona_message(role, persona_name, value)
    return _jev_only_global_message(role, value, names)


def missing_model_warnings(
    entries: Iterable[Tuple[str, Optional[str]]],
    *,
    persona_name: Optional[str] = None,
    override_model: Optional[str] = None,
    affected_persona_names: Optional[Mapping[str, Sequence[str]]] = None,
) -> List[Dict[str, str]]:
    """設定されたモデル名のうち、そのままでは役割が働かないものを画面の警告にして返す。

    見るのは二通り — 定義が見つからない値と、定義はあるのにその役割の宛先として
    噛み合っていない値 (反射判断の役割に反射判断ができない宛先、または会話や要約の
    役割に反射判断専用の宛先)。

    Args:
        entries: ``(役割, 設定値)`` の並び。役割は ``_ROLE_CONFIG_LOOKUPS`` のキー。
            設定値が None / 空文字の役割は未設定として検査しない。
        persona_name: 渡すとペルソナ単位の文面 (その名前で呼ぶ)、省略すると
            グローバル設定単位の文面になる。
        override_model: チャット画面のモデル一時上書きが有効なら、そのモデル名。
            標準モデルの文面を「止まっています」ではなく「上書きを解除すると止まる」にする。
        affected_persona_names: グローバル設定単位の文面で、役割ごとに「その値を
            使っているペルソナ (個別の値を持たないペルソナ)」の名前。分からないときは省略する。

    一つの役割の検査が例外を出しても、残りの役割の検査は続ける。例外を出した値は
    「確かめられなかった = 定義なし」として、その役割の警告に出す。
    """
    warnings: List[Dict[str, str]] = []
    for role, value in entries:
        if not value or not str(value).strip():
            continue
        try:
            defined = role_model_is_defined(role, value)
            # 定義があっても、その役割の宛先として噛み合っていないことがある
            # (反射判断に通常の LLM や宛先の宣言が欠けたモデル、逆に会話の役割に
            # 反射判断専用の宛先)。env の直書きなど保存の関所を通らない経路で
            # 入った値もここで拾う。
            check = _ROLE_DESTINATION_CHECKS.get(role)
            mismatched = bool(defined and check is not None and check(role, value))
        except Exception:
            # 確かめられなかった値は「定義なし」と同じ扱いにして画面に出す。黙って
            # 飛ばすと、検査そのものが壊れている間だけ警告が消え、設定が正しいのと
            # 見分けがつかない。保存と一時上書きの関所も「確かめられない値は断って
            # 知らせる」側に倒してあるので、警告だけ逆に倒さない。
            LOGGER.warning(
                "Model config check failed (role=%s value=%r persona=%s); "
                "warning about it as if it were undefined.",
                role, value, persona_name, exc_info=True,
            )
            defined = False
            mismatched = False
        if defined and not mismatched:
            continue

        if mismatched:
            message = _mismatch_message(
                role, value, persona_name, (affected_persona_names or {}).get(role),
            )
            LOGGER.warning(
                "Model config is not a usable destination for this role "
                "(role=%s value=%r persona=%s).",
                role, value, persona_name,
            )
        else:
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
