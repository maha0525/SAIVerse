"""ペルソナが話す標準モデルの決め方と、設定を変えたときの反映。

設計の正本: docs/intent/persona_model_selection.md

このモジュールが持つもの:

- 決め方 (:func:`resolve_speaking_model`): チャット画面のモデル一時上書き →
  ペルソナ個別の標準モデル → グローバル設定の標準モデル → 組み込みの既定モデル。
  空でない最初のものを使う。選ばれたモデルの設定ファイルが無ければ、次の候補へ
  進まず「使えない」と決める。話す標準モデルを決める場所はここだけで、起動時の
  読み込み・ペルソナの作成・設定の保存・一時上書き・モデルやプロバイダの設定の
  読み直しが全部これを使う。
- 決め直しと当てはめ (:func:`reapply_speaking_models`): 先に各ペルソナの DB 行と
  使うモデルの定義を読み、それから一人ずつ値を書き換えて、作ってあった接続を
  捨てる。新しい接続は次の返事を始めたあとに作られる。一人の失敗でほかの人を
  止めず、切り替えられなかった人の名前を返す。
- ロック (:data:`MODEL_SETTINGS_LOCK`): .env の書き換え・決め直しと当てはめ・
  一時上書きの設定と解除 (一時上書きのパラメータの変更を含む)・ペルソナ設定の保存と
  ペルソナの作成のモデル部分・モデルやプロバイダの設定の読み直し (定義の差し替えから
  決め直しまで) で共有する。中で行うのは設定の読み書き・DB の読み取り・値の書き換え
  だけで、Beat ロックは待たない。
- 返事の始まりに決めるモデルと接続 (:class:`ReplyModelBinding`): 書いている途中の
  返事は、始めたときのモデルと接続を最後まで使う。次の返事から新しい設定になる。
- 設定ファイルの無いモデルの名前を保存しない検査と、画面へ出す知らせの文面。
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

from llm_clients.exceptions import LLMError, ModelUnavailableError
from saiverse.model_defaults import (
    BUILTIN_DEFAULT_LITE_MODEL,
    MODEL_ROLE_DESCRIPTIONS,
    MODEL_ROLES,
    role_model_is_defined,
)

LOGGER = logging.getLogger(__name__)

#: 設定の書き換えと決め直しを一件ずつ順番に処理するロック (決まったこと 5)。
#: 再入可能: .env の書き換えやペルソナ設定の保存、モデルやプロバイダの設定の読み直しの
#: 中から決め直しを呼ぶ。取る順番はこのロックが先で、ペルソナの接続のロック
#: (``_model_client_lock``) が後。
MODEL_SETTINGS_LOCK = threading.RLock()

#: 話す標準モデルをどの設定から決めたか
SOURCE_OVERRIDE = "override"   # チャット画面のモデル一時上書き
SOURCE_PERSONA = "persona"     # ペルソナ個別の標準モデル (DB の AI.DEFAULT_MODEL)
SOURCE_GLOBAL = "global"       # グローバル設定の標準モデル (環境変数)
SOURCE_BUILTIN = "builtin"     # どれも空のときの組み込みの既定モデル

TIER_STANDARD = "standard"
TIER_LIGHTWEIGHT = "lightweight"

REASON_MISSING = "missing"          # 設定ファイルが無い
REASON_UNREACHABLE = "unreachable"  # 設定ファイルはあるが接続を作れない

#: 使えないモデルを指すペルソナに入れておく文脈長 (どこでも使われない値だが、
#: 未設定で 0 にすると窓の計算の退避先が崩れるので、既定と同じ値にしておく)。
_UNDEFINED_CONTEXT_LENGTH = 120000

_ROLE_BY_ENV_KEY: Dict[str, str] = {env_key: role for role, env_key in MODEL_ROLES.items()}


def _clean(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _display_name(persona: Any, fallback: Optional[str] = None) -> str:
    return (
        _clean(getattr(persona, "persona_name", None))
        or _clean(fallback)
        or _clean(getattr(persona, "persona_id", None))
        or "ペルソナ"
    )


def _persona_lock(persona: Any):
    lock = getattr(persona, "_model_client_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


def _label(role: str) -> str:
    return MODEL_ROLE_DESCRIPTIONS[role]["label"]


# ---------------------------------------------------------------------------
# 決め方
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpeakingModelChoice:
    """決め方が指す、ペルソナが話す標準モデル。

    Attributes:
        model: モデル名 (設定キー)。
        source: どの設定から決めたか (``SOURCE_*``)。
        defined: そのモデルの設定ファイルがあるか。False のペルソナは止まる。
    """

    model: str
    source: str
    defined: bool


def global_default_model_setting() -> Optional[str]:
    """グローバル設定の標準モデル。空・未設定なら None。"""
    return _clean(os.getenv(MODEL_ROLES["default_model"]))


def global_lightweight_model_setting() -> Optional[str]:
    """グローバル設定の軽量モデル。空・未設定なら None。"""
    return _clean(os.getenv(MODEL_ROLES["lightweight_model"]))


def resolve_speaking_model(
    *,
    override: Optional[str],
    persona_default: Optional[str],
    global_default: Optional[str],
) -> SpeakingModelChoice:
    """話す標準モデルを決める。話す標準モデルを決める順番はここにだけ書く。

    空でない最初の候補を使い、その設定ファイルが無ければ次の候補へ進まない
    (``defined=False``)。どれも空なら組み込みの既定モデル。
    """
    for source, value in (
        (SOURCE_OVERRIDE, override),
        (SOURCE_PERSONA, persona_default),
        (SOURCE_GLOBAL, global_default),
    ):
        name = _clean(value)
        if name is not None:
            return SpeakingModelChoice(name, source, role_model_is_defined("default_model", name))
    return SpeakingModelChoice(
        BUILTIN_DEFAULT_LITE_MODEL,
        SOURCE_BUILTIN,
        role_model_is_defined("default_model", BUILTIN_DEFAULT_LITE_MODEL),
    )


def speaking_model_definition(
    choice: SpeakingModelChoice,
) -> Tuple[SpeakingModelChoice, str, int]:
    """決めたモデルのプロバイダと文脈長を返す。

    決めたあとで読み直しにより定義が消えていたら、使えないと決め直して返す。
    使えないモデルのプロバイダは空文字。
    """
    if choice.defined:
        from saiverse import model_configs

        try:
            return (
                choice,
                model_configs.get_model_provider(choice.model),
                model_configs.get_context_length(choice.model),
            )
        except ValueError:
            choice = replace(choice, defined=False)
    return choice, "", _UNDEFINED_CONTEXT_LENGTH


def live_model_override(host: Any) -> Tuple[Optional[str], Dict[str, Any]]:
    """いま有効なチャット画面のモデル一時上書きと、そのパラメータ。

    一時上書きの実体は SAIVerseManager の ``model`` にある。AdminService /
    RuntimeService は起動時に写した値を持つので、写しではなく ``manager`` の値を読む。
    """
    manager = getattr(host, "manager", None)
    owner = manager if manager is not None and hasattr(manager, "model") else host
    model = _clean(getattr(owner, "model", None))
    if model is None:
        return None, {}
    overrides = getattr(owner, "model_parameter_overrides", None)
    return model, dict(overrides) if isinstance(overrides, dict) else {}


def initial_speaking_model(
    host: Any, *, persona_default: Optional[str] = None,
) -> Tuple[SpeakingModelChoice, str, int, Dict[str, Any]]:
    """いまの設定から決めたモデル・プロバイダ・文脈長・一時上書きのパラメータ。"""
    override, overrides = live_model_override(host)
    choice = resolve_speaking_model(
        override=override,
        persona_default=persona_default,
        global_default=global_default_model_setting(),
    )
    choice, provider, context_length = speaking_model_definition(choice)
    return (
        choice, provider, context_length,
        overrides if choice.source == SOURCE_OVERRIDE else {},
    )


def attach_speaking_model_choice(
    persona: Any, choice: SpeakingModelChoice, parameter_overrides: Optional[Dict[str, Any]],
) -> None:
    """構築したばかりのペルソナに、決め方の結果を持たせる (値は構築時に渡し済み)。"""
    persona.speaking_model_choice = choice
    if choice.source == SOURCE_OVERRIDE and parameter_overrides:
        persona._pending_parameter_overrides = dict(parameter_overrides)


def apply_speaking_model(
    persona: Any,
    choice: SpeakingModelChoice,
    provider: str,
    context_length: int,
    *,
    parameter_overrides: Optional[Dict[str, Any]] = None,
    drop_connections: bool = False,
) -> bool:
    """決めたモデルをペルソナに当てはめる。値を書き換え、接続を捨てるだけ。

    変わっていなければ何もしない (``drop_connections`` のときは接続だけ捨てる)。
    返り値は値を書き換えたか。
    """
    overrides = (
        dict(parameter_overrides)
        if choice.source == SOURCE_OVERRIDE and parameter_overrides else None
    )
    with _persona_lock(persona):
        unchanged = (
            getattr(persona, "speaking_model_choice", None) == choice
            and getattr(persona, "model", None) == choice.model
            and getattr(persona, "provider", None) == provider
            and getattr(persona, "context_length", None) == context_length
            and (getattr(persona, "_pending_parameter_overrides", None) or None) == overrides
        )
        if not unchanged:
            persona.set_model(choice.model, context_length, provider, overrides)
        if drop_connections:
            persona.drop_llm_clients()
        persona.speaking_model_choice = choice
    return not unchanged


def _apply_lightweight_model(persona: Any, value: Optional[str]) -> None:
    setter = getattr(persona, "set_lightweight_model", None)
    if callable(setter):
        setter(value)
    else:
        persona.lightweight_model = value


# ---------------------------------------------------------------------------
# 決め直しと当てはめ
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonaModelRow:
    """決め直しに使う、ペルソナの DB 行のモデルの欄。"""

    persona_id: str
    name: Optional[str]
    default_model: Optional[str]
    lightweight_model: Optional[str]
    memory_weave_model: Optional[str]


def read_persona_model_rows(
    session_factory: Any,
    *,
    persona_ids: Optional[Iterable[str]] = None,
    city_id: Optional[int] = None,
) -> Dict[str, PersonaModelRow]:
    """ペルソナの DB 行からモデルの欄を読む (並びは DB が返した順)。"""
    from database.models import AI as AIModel

    db = session_factory()
    try:
        query = db.query(
            AIModel.AIID,
            AIModel.AINAME,
            AIModel.DEFAULT_MODEL,
            AIModel.LIGHTWEIGHT_MODEL,
            AIModel.MEMORY_WEAVE_MODEL,
        )
        if persona_ids is not None:
            query = query.filter(AIModel.AIID.in_(list(persona_ids)))
        if city_id is not None:
            query = query.filter(AIModel.HOME_CITYID == city_id)
        return {row[0]: PersonaModelRow(*row) for row in query.all()}
    finally:
        db.close()


@dataclass(frozen=True)
class UnswitchedPersona:
    """切り替えられなかったペルソナ。前のモデルのまま話している。"""

    name: str
    model: str


@dataclass
class ReapplyResult:
    unswitched: List[UnswitchedPersona] = field(default_factory=list)

    @property
    def unswitched_names(self) -> List[str]:
        return [item.name for item in self.unswitched]

    def notices(self) -> List[str]:
        return [unswitched_persona_message(item.name, item.model) for item in self.unswitched]


def _unswitched(persona: Any, name: Optional[str] = None) -> UnswitchedPersona:
    return UnswitchedPersona(
        _display_name(persona, name), str(getattr(persona, "model", "") or ""),
    )


def _invalidate_cold_sweep() -> None:
    # 記憶の整理の見張りは「前回と同じ状態なら結果も同じ」で素通しする。モデルを
    # 選び直しても行が動かないと再試行されないので、記録を捨てる。
    from sea.session_lifecycle import invalidate_cold_sweep_fingerprints

    invalidate_cold_sweep_fingerprints()


def reapply_speaking_models(
    manager: Any,
    *,
    drop_connections: bool = False,
    persona_ids: Optional[Iterable[str]] = None,
) -> ReapplyResult:
    """いまの設定から全員 (または ``persona_ids`` の人) の話すモデルを決め直して当てはめる。

    先に必要なもの (DB 行とモデルの定義) を読み、それから一人ずつ当てはめる。当てはめは
    値の書き換えと接続の破棄だけ。一人の失敗でほかの人を止めない。DB を読めなかったとき
    は誰も切り替えず、全員を切り替えられなかった人として返す。

    ``drop_connections``: モデルやプロバイダの設定を読み直したあと。モデル名が
    変わらなくても接続先の設定が変わりうるので、全員の接続を捨てる。
    """
    result = ReapplyResult()
    personas = getattr(manager, "personas", None) or {}
    wanted = set(persona_ids) if persona_ids is not None else None
    with MODEL_SETTINGS_LOCK:
        targets = [
            (pid, persona) for pid, persona in list(personas.items())
            if wanted is None or pid in wanted
        ]
        if not targets:
            return result
        override, overrides = live_model_override(manager)
        global_default = global_default_model_setting()
        try:
            rows = read_persona_model_rows(
                manager.SessionLocal, persona_ids=[pid for pid, _ in targets],
            )
        except Exception:
            LOGGER.warning(
                "[model-selection] could not read persona model settings from the DB; "
                "no persona was switched", exc_info=True,
            )
            result.unswitched.extend(_unswitched(persona) for _, persona in targets)
            return result

        plans = []
        for pid, persona in targets:
            row = rows.get(pid)
            if row is None:
                LOGGER.warning(
                    "[model-selection] persona %s has no DB row; not switched", pid,
                )
                result.unswitched.append(_unswitched(persona))
                continue
            try:
                choice = resolve_speaking_model(
                    override=override,
                    persona_default=row.default_model,
                    global_default=global_default,
                )
                choice, provider, context_length = speaking_model_definition(choice)
            except Exception:
                LOGGER.warning(
                    "[model-selection] could not decide the speaking model for %s; "
                    "not switched", pid, exc_info=True,
                )
                result.unswitched.append(_unswitched(persona, row.name))
                continue
            plans.append((pid, persona, row, choice, provider, context_length))

        for pid, persona, row, choice, provider, context_length in plans:
            try:
                with _persona_lock(persona):
                    changed = apply_speaking_model(
                        persona, choice, provider, context_length,
                        parameter_overrides=overrides,
                        drop_connections=drop_connections,
                    )
                    _apply_lightweight_model(persona, row.lightweight_model)
                    persona.memory_weave_model = row.memory_weave_model
            except Exception:
                LOGGER.warning(
                    "[model-selection] failed to switch persona %s; it keeps speaking "
                    "with '%s'", pid, getattr(persona, "model", None), exc_info=True,
                )
                result.unswitched.append(_unswitched(persona, row.name))
                continue
            if changed:
                LOGGER.info(
                    "[model-selection] persona %s now speaks with '%s' (source=%s defined=%s)",
                    pid, choice.model, choice.source, choice.defined,
                )
            if not choice.defined:
                LOGGER.warning(
                    "[model-selection] persona %s: speaking model '%s' (source=%s) has no "
                    "definition; the persona is stopped until it is reselected "
                    "(no substitute model)", pid, choice.model, choice.source,
                )
    _invalidate_cold_sweep()
    return result


def register_new_persona(
    host: Any, persona_id: str, persona: Any, initial_choice: SpeakingModelChoice,
) -> None:
    """作ったペルソナを世界に登録する。登録と決め直しは同じロックの中で行う。

    構築に使ったモデルは、ロックの外で決めた値。登録の直前にロックの中で決め直し、
    その間に設定が変わっていれば当てはめ直す。登録がロックの中にあるので、同時に
    保存された設定の決め直しは、このペルソナを含めて行われるか、このペルソナの
    決め直しより前に済んでいるかのどちらかになる。新しいペルソナは個別の標準モデルを
    持たない (一時上書きのモデルを個別の標準モデルとして保存しない)。
    """
    with MODEL_SETTINGS_LOCK:
        try:
            choice, provider, context_length, overrides = initial_speaking_model(host)
            if choice == initial_choice:
                attach_speaking_model_choice(persona, choice, overrides)
            else:
                apply_speaking_model(
                    persona, choice, provider, context_length, parameter_overrides=overrides,
                )
        except Exception:
            LOGGER.warning(
                "[model-selection] could not re-decide the speaking model for the new "
                "persona %s; it keeps '%s'", persona_id, initial_choice.model, exc_info=True,
            )
            attach_speaking_model_choice(persona, initial_choice, None)
        host.personas[persona_id] = persona


def reapply_after_config_reload() -> ReapplyResult:
    """モデルやプロバイダの設定を読み直したあとに、全員を決め直して接続を捨てる。

    読み直しの入口 (saiverse/model_configs.py と saiverse/provider_configs.py の
    reload_configs) から、設定のロックを持ったまま呼ぶ。返り値は切り替えられなかった
    人で、読み直しを起こした画面がその場で知らせる。世界がまだ起動していないとき、
    または決め直しそのものが例外を出したときは、空の結果を返す (ログは残す)。
    """
    try:
        from saiverse import app_state
    except Exception:
        return ReapplyResult()
    # 要約の接続は、ペルソナの決め直しの成否に関係なく捨てる。読み直した設定で
    # 作り直させないと、決め直しが途中で失敗した回だけ要約が古い接続先を使い続ける。
    try:
        from saiverse.media_summary import invalidate_summary_client

        invalidate_summary_client()
    except Exception:
        LOGGER.warning("[model-selection] failed to drop media summary clients", exc_info=True)
    manager = getattr(app_state, "manager", None)
    if manager is None:
        return ReapplyResult()
    try:
        result = reapply_speaking_models(manager, drop_connections=True)
    except Exception:
        LOGGER.warning(
            "[model-selection] failed to re-decide speaking models after a config reload",
            exc_info=True,
        )
        return ReapplyResult()
    for item in result.unswitched:
        LOGGER.warning(
            "[model-selection] %s was not switched after a config reload; still '%s'",
            item.name, item.model,
        )
    return result


# ---------------------------------------------------------------------------
# 設定ファイルの無いモデルの名前を保存しない
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RejectedModelSetting:
    env_key: str
    role: str
    value: str


def _is_defined_for_saving(role: str, value: str) -> bool:
    try:
        return role_model_is_defined(role, value)
    except Exception:
        # 確かめられない名前は保存しない (保存したあとで「無かった」と分かっても遅い)
        LOGGER.warning(
            "[model-selection] definition lookup failed (role=%s value=%r); not saving it",
            role, value, exc_info=True,
        )
        return False


def split_undefined_model_updates(
    updates: Mapping[str, str],
) -> Tuple[Dict[str, str], List[RejectedModelSetting]]:
    """環境変数の保存要求を、保存するものと、設定ファイルが無いので保存しないものに分ける。

    モデルの役割の変数 (``MODEL_ROLES``) だけを検べる。空の値は受け付ける (設定を外す)。
    いまの値と同じ名前はそもそも新しい保存ではないので検べない。
    """
    accepted: Dict[str, str] = {}
    rejected: List[RejectedModelSetting] = []
    for key, value in updates.items():
        role = _ROLE_BY_ENV_KEY.get(key)
        name = _clean(value)
        if role is None or name is None or name == _clean(os.environ.get(key)):
            accepted[key] = value
            continue
        if _is_defined_for_saving(role, name):
            accepted[key] = value
        else:
            rejected.append(RejectedModelSetting(key, role, name))
    return accepted, rejected


def model_role_env_keys() -> frozenset:
    return frozenset(MODEL_ROLES.values())


# ---------------------------------------------------------------------------
# 画面へ出す文面
# ---------------------------------------------------------------------------


def unswitched_persona_message(name: str, model: str) -> str:
    return (
        f"{name}は新しい標準モデルに切り替えられなかったため、いまも '{model}' で話しています。"
        "もう一度保存し直すか、再起動すると切り替わります。"
    )


def undefined_override_message(model: str) -> str:
    return (
        f"'{model}' というモデルは SAIVerse にないため、チャット画面のモデル一時上書きには"
        "使えません。モデル管理の画面にあるモデルから選び直してください。"
    )


def override_missing_message(model: str) -> str:
    return (
        f"チャット画面のモデル一時上書き '{model}' は SAIVerse にないため、ペルソナは止まっています。"
        "チャット画面でモデルを選び直すか一時上書きを解除すると、再起動しなくても話せるようになります。"
    )


def rejected_global_model_message(rejected: RejectedModelSetting, manager: Any) -> str:
    """グローバル設定で保存を断ったときの知らせ。どの設定を保存しなかったかと、いま使っているモデル。"""
    head = (
        f"'{rejected.value}' というモデルは SAIVerse にないため、"
        f"グローバル設定の{_label(rejected.role)}は保存しませんでした。"
    )
    current = _clean(os.environ.get(rejected.env_key))
    if rejected.role == "default_model":
        override, _ = live_model_override(manager) if manager is not None else (None, {})
        if override:
            return head + f"いまはチャット画面のモデル一時上書き '{override}' で話しています。"
        model = current or BUILTIN_DEFAULT_LITE_MODEL
        if not _is_defined_for_saving("default_model", model):
            return head + (
                f"個別の標準モデルを持たないペルソナは、'{model}' が SAIVerse にないため"
                "止まったままです。"
            )
        return head + f"個別の標準モデルを持たないペルソナは、いまも '{model}' で話しています。"
    if current:
        return head + f"いまも '{current}' を使っています。"
    return head + f"いまも組み込みの既定モデル '{BUILTIN_DEFAULT_LITE_MODEL}' を使っています。"


def rejected_persona_model_message(
    persona_name: str,
    role: str,
    value: str,
    *,
    stored: Optional[str],
    persona: Any = None,
) -> str:
    """ペルソナ設定で保存を断ったときの知らせ。"""
    head = (
        f"'{value}' というモデルは SAIVerse にないため、{persona_name}の{_label(role)}は"
        "保存しませんでした。"
    )
    if role == "default_model" and persona is not None:
        model = str(getattr(persona, "model", "") or "")
        choice = getattr(persona, "speaking_model_choice", None)
        if isinstance(choice, SpeakingModelChoice) and choice.model == model and not choice.defined:
            return head + f"{persona_name}は、'{model}' が SAIVerse にないため止まったままです。"
        if model:
            return head + f"{persona_name}はいまも '{model}' で話しています。"
    if stored:
        return head + f"いまの設定 '{stored}' のままです。"
    return head + "個別には選んでいないまま (グローバル設定のモデルを使う) です。"


def reply_unavailable_message(
    name: str, *, role: str, model: str, reason: str, source: Optional[str],
) -> str:
    """話そうとして止まったときにチャット画面へ出す文面。

    誰が・どのモデルが無いか (または繋げないか)・どこで選び直すか・再起動は要らない、
    の四つを必ず入れる。
    """
    if role == "default_model":
        if reason == REASON_UNREACHABLE:
            return (
                f"{name}の標準モデル '{model}' に繋げなかったため、返事を書けませんでした。"
                "API キーなどの接続の設定を確かめるか、標準モデルを選び直してください。再起動は要りません。"
            )
        if source == SOURCE_PERSONA:
            return (
                f"{name}が選んでいた標準モデル '{model}' は SAIVerse にありません。"
                "ペルソナ設定で標準モデルを選び直すと、再起動しなくても話せるようになります。"
            )
        if source == SOURCE_GLOBAL:
            return (
                f"{name}が使うグローバル設定の標準モデル '{model}' は SAIVerse にありません。"
                "グローバル設定の「モデルロール」で標準モデルを選び直すと、再起動しなくても話せるようになります。"
            )
        if source == SOURCE_BUILTIN:
            return (
                f"{name}が使う組み込みの既定モデル '{model}' は SAIVerse にありません。"
                "グローバル設定の「モデルロール」で標準モデルを選ぶと、再起動しなくても話せるようになります。"
            )
        if source == SOURCE_OVERRIDE:
            return (
                f"{name}が使うチャット画面のモデル一時上書き '{model}' は SAIVerse にありません。"
                "チャット画面でモデルを選び直すか一時上書きを解除すると、再起動しなくても話せるようになります。"
            )
        return (
            f"{name}の標準モデル '{model}' は SAIVerse にありません。"
            "ペルソナ設定 (個別に選んでいない場合はグローバル設定の「モデルロール」) で標準モデルを"
            "選び直すと、再起動しなくても話せるようになります。"
        )
    if reason == REASON_UNREACHABLE:
        return (
            f"{name}の軽量モデル '{model}' に繋げなかったため、返事の途中の作業ができませんでした。"
            "API キーなどの接続の設定を確かめるか、軽量モデルを選び直してください。再起動は要りません。"
        )
    where = {
        SOURCE_PERSONA: "ペルソナ設定",
        SOURCE_GLOBAL: "グローバル設定の「モデルロール」",
        SOURCE_BUILTIN: "グローバル設定の「モデルロール」",
    }.get(source, "ペルソナ設定 (個別に選んでいない場合はグローバル設定の「モデルロール」)")
    return (
        f"{name}の軽量モデル '{model}' は SAIVerse にないため、返事の途中の作業ができませんでした。"
        f"{where}で軽量モデルを選び直すと、再起動しなくても続けられます。"
    )


# ---------------------------------------------------------------------------
# 返事の始まりに決めるモデルと接続
# ---------------------------------------------------------------------------


@dataclass
class _TierPin:
    """一つの段 (標準 / 軽量) について、返事の始まりに決めたもの。"""

    tier: str
    model: str
    source: Optional[str]
    config: Optional[Dict[str, Any]]
    client: Any = None


_ACTIVE_BINDING: contextvars.ContextVar[Optional["ReplyModelBinding"]] = contextvars.ContextVar(
    "saiverse_reply_model_binding", default=None,
)


class ReplyModelBinding:
    """返事の始まりに決めた、その返事で使うモデルと接続。

    返事 (Pulse) の始まりに :meth:`capture` で一度だけ作り、返事の中の LLM 呼び出しと
    送る内容の準備が全部これを使う。途中でペルソナのモデルやプロバイダの設定が変わり、
    ペルソナが持つ接続が捨てられても、この返事は始めたときのモデル・設定・接続を使い
    続ける。

    - モデル名とその定義 (設定の中身) は始まりに控える。接続は要ったときに作る
      (使わない段の接続を作らない)。
    - ペルソナの設定が始まりから変わっていなければ、ペルソナが持つ接続を使う
      (返事をまたいで同じ接続を使い回す)。変わっていたら、始まりに控えた定義から
      この返事専用の接続を作る。
    - 使えない (定義が無い・繋げない) ときは :class:`ModelUnavailableError` を出す。
      代わりのモデルへは回さない。

    返事の中への伝え方: ``state["_model_binding"]``、``PulseContext.model_binding``
    (スペルが別スレッドで走っても届く)、:func:`reply_binding_scope` の文脈変数
    (同じスレッドで呼ばれる返事の後片付け、例えば記憶の整理にも届く)。
    """

    def __init__(
        self,
        persona: Any,
        *,
        standard: _TierPin,
        lightweight: _TierPin,
        token: Optional[object],
        choice: Optional[SpeakingModelChoice],
        parameter_overrides: Optional[Dict[str, Any]],
    ) -> None:
        self._persona = persona
        self._pins: Dict[str, _TierPin] = {TIER_STANDARD: standard, TIER_LIGHTWEIGHT: lightweight}
        self._token = token
        self._choice = choice
        self._parameter_overrides = dict(parameter_overrides) if parameter_overrides else None
        self._lock = threading.RLock()
        self._other_clients: Dict[str, Any] = {}
        self._structured_clients: Dict[str, Any] = {}
        persona_id = getattr(persona, "persona_id", None)
        self.persona_id: Optional[str] = persona_id if isinstance(persona_id, str) else None

    @classmethod
    def capture(cls, persona: Any) -> "ReplyModelBinding":
        """いまのペルソナの設定から、この返事で使うモデルを控える。"""
        from saiverse import model_configs

        with _persona_lock(persona):
            token = getattr(persona, "_model_settings_token", None)
            standard_model = str(getattr(persona, "model", "") or "")
            persona_lightweight = _clean(getattr(persona, "lightweight_model", None))
            choice = getattr(persona, "speaking_model_choice", None)
            overrides = getattr(persona, "_pending_parameter_overrides", None)
            standard_client = getattr(persona, "_llm_client", None) if token is not None else None
            lightweight_client = (
                getattr(persona, "_lightweight_llm_client", None)
                if token is not None and persona_lightweight else None
            )
        if not isinstance(choice, SpeakingModelChoice) or choice.model != standard_model:
            # 決め方を通っていない値 (部分的に組んだペルソナなど) — 定義の有無は
            # 接続を作るときの失敗で判定する
            choice = None
        if persona_lightweight is not None:
            lightweight_model, lightweight_source = persona_lightweight, SOURCE_PERSONA
        else:
            global_lightweight = global_lightweight_model_setting()
            if global_lightweight is not None:
                lightweight_model, lightweight_source = global_lightweight, SOURCE_GLOBAL
            else:
                lightweight_model, lightweight_source = BUILTIN_DEFAULT_LITE_MODEL, SOURCE_BUILTIN
        registry = model_configs.MODEL_CONFIGS
        standard = _TierPin(
            TIER_STANDARD, standard_model, choice.source if choice else None,
            registry.get(standard_model) if standard_model else None, standard_client,
        )
        lightweight = _TierPin(
            TIER_LIGHTWEIGHT, lightweight_model, lightweight_source,
            registry.get(lightweight_model), lightweight_client,
        )
        return cls(
            persona,
            standard=standard,
            lightweight=lightweight,
            token=token,
            choice=choice,
            parameter_overrides=overrides if isinstance(overrides, dict) else None,
        )

    # -- 読み出し ---------------------------------------------------------

    def model_for(self, tier: str) -> str:
        return self._pins[tier].model

    def check_defined(self, tier: str) -> None:
        """その段のモデルの設定ファイルが無ければ、いま止める (接続は作らない)。"""
        pin = self._pins[tier]
        if tier == TIER_STANDARD:
            if self._choice is None:
                return
            if not self._choice.defined or pin.config is None:
                raise self._unavailable(pin, REASON_MISSING)
            return
        if pin.config is not None:
            return
        from saiverse import model_configs

        try:
            model_configs.get_model_provider(pin.model)
        except ValueError as exc:
            raise self._unavailable(pin, REASON_MISSING, exc) from exc

    def client_for(self, tier: str) -> Any:
        """その段の接続。一つの返事の中では同じ接続を返す。"""
        pin = self._pins[tier]
        with self._lock:
            if pin.client is None:
                pin.client = self._obtain(pin)
            return pin.client

    def client_for_model(self, tier: str, model_key: str) -> Any:
        """``model_key`` のモデルの接続。返事で決めたモデルと食い違わせない。"""
        if model_key == self._pins[tier].model:
            return self.client_for(tier)
        other = TIER_LIGHTWEIGHT if tier == TIER_STANDARD else TIER_STANDARD
        if model_key == self._pins[other].model:
            return self.client_for(other)
        from saiverse import model_configs

        with self._lock:
            client = self._other_clients.get(model_key)
            if client is None:
                pin = _TierPin(tier, model_key, None, model_configs.MODEL_CONFIGS.get(model_key))
                client = self._create_private(pin)
                self._other_clients[model_key] = client
            return client

    def structured_output_client(self, model_key: str) -> Any:
        """標準モデルが構造化出力に対応していないときに使う、軽量モデルの接続。

        この使い分けは失敗の代わりではなく、モデルの能力に合わせた使い分け。
        繋げなければ止める (元の段の接続へは戻らない)。
        """
        from saiverse import model_configs

        with self._lock:
            client = self._structured_clients.get(model_key)
            if client is None:
                lightweight = self._pins[TIER_LIGHTWEIGHT]
                if model_key == lightweight.model:
                    pin = replace(lightweight, client=None)
                else:
                    pin = _TierPin(
                        TIER_LIGHTWEIGHT, model_key, None,
                        model_configs.MODEL_CONFIGS.get(model_key),
                    )
                client = self._create_private(pin)
                self._structured_clients[model_key] = client
            return client

    # -- 接続を作る -------------------------------------------------------

    def _persona_unchanged(self, pin: _TierPin) -> bool:
        if self._token is not None:
            return getattr(self._persona, "_model_settings_token", None) is self._token
        # 世代札を持たないペルソナ (部分的に組んだペルソナ) は名前で見る
        if pin.tier == TIER_STANDARD:
            return str(getattr(self._persona, "model", "") or "") == pin.model
        return _clean(getattr(self._persona, "lightweight_model", None)) == pin.model

    def _obtain(self, pin: _TierPin) -> Any:
        persona = self._persona
        if pin.tier == TIER_STANDARD:
            self.check_defined(TIER_STANDARD)
            if self._persona_unchanged(pin):
                try:
                    client = persona.llm_client
                except Exception as exc:
                    raise self._classify_failure(pin, exc) from exc
                if client is None:
                    persona_name = getattr(persona, "persona_name", "unknown")
                    raise LLMError(
                        f"LLM client is not initialized for persona '{persona_name}' (model={pin.model})",
                        user_message=(
                            f"ペルソナ「{persona_name}」のLLMクライアントが初期化されていません。"
                            "チャットオプションでモデルを選択してください。"
                        ),
                    )
                return client
            return self._create_private(pin)
        if pin.source == SOURCE_PERSONA and self._persona_unchanged(pin):
            client = getattr(persona, "lightweight_llm_client", None)
            if client is not None:
                return client
            error = getattr(persona, "_lightweight_llm_client_error", None)
            if isinstance(error, BaseException):
                raise self._classify_failure(pin, error) from error
        return self._create_private(pin)

    def _create_private(self, pin: _TierPin) -> Any:
        """返事の始まりに控えた定義から、この返事専用の接続を作る。"""
        from saiverse import model_configs

        config = pin.config
        try:
            if isinstance(config, dict):
                provider = config.get("provider", "ollama")
                context_length = int(config.get("context_length", 120000))
            else:
                provider = model_configs.get_model_provider(pin.model)
                context_length = model_configs.get_context_length(pin.model)
        except ValueError as exc:
            raise self._unavailable(pin, REASON_MISSING, exc) from exc
        try:
            from llm_clients import get_llm_client

            if isinstance(config, dict):
                client = get_llm_client(pin.model, provider, context_length, config)
            else:
                client = get_llm_client(pin.model, provider, context_length)
        except Exception as exc:
            raise self._unavailable(pin, REASON_UNREACHABLE, exc) from exc
        if pin.tier == TIER_STANDARD and self._parameter_overrides:
            configure = getattr(self._persona, "_configure_client_parameters", None)
            if callable(configure):
                configure(client, pin.model, self._parameter_overrides)
        return client

    def _classify_failure(self, pin: _TierPin, exc: BaseException) -> ModelUnavailableError:
        if isinstance(exc, ModelUnavailableError):
            return exc
        from saiverse import model_configs

        reason = REASON_UNREACHABLE if pin.model in model_configs.MODEL_CONFIGS else REASON_MISSING
        return self._unavailable(pin, reason, exc)

    def _unavailable(
        self, pin: _TierPin, reason: str, exc: Optional[BaseException] = None,
    ) -> ModelUnavailableError:
        role = "default_model" if pin.tier == TIER_STANDARD else "lightweight_model"
        name = _display_name(self._persona)
        detail = f": {type(exc).__name__}: {exc}" if exc is not None else ""
        return ModelUnavailableError(
            f"{role} '{pin.model}' is {reason} for persona {self.persona_id}{detail}",
            role=role,
            reason=reason,
            model=pin.model,
            persona_id=self.persona_id,
            original_error=exc if isinstance(exc, Exception) else None,
            user_message=reply_unavailable_message(
                name, role=role, model=pin.model, reason=reason, source=pin.source,
            ),
        )


def find_reply_binding(
    *,
    state: Optional[Mapping[str, Any]] = None,
    pulse_context: Any = None,
    persona: Any = None,
) -> Optional[ReplyModelBinding]:
    """書いている途中の返事で決めたモデルと接続を探す。返事の外なら None。"""
    candidates: List[Any] = []
    if isinstance(state, Mapping):
        candidates.append(state.get("_model_binding"))
        candidates.append(getattr(state.get("_pulse_context"), "model_binding", None))
    if pulse_context is not None:
        candidates.append(getattr(pulse_context, "model_binding", None))
    candidates.append(_ACTIVE_BINDING.get())
    persona_id = getattr(persona, "persona_id", None) if persona is not None else None
    for candidate in candidates:
        if not isinstance(candidate, ReplyModelBinding):
            continue
        if persona_id is None or candidate.persona_id is None or candidate.persona_id == persona_id:
            return candidate
    return None


def enter_reply_binding(binding: ReplyModelBinding) -> contextvars.Token:
    """返事の間、同じスレッドで呼ばれる処理にも ``binding`` が届くようにする (札を返す)。"""
    return _ACTIVE_BINDING.set(binding)


def exit_reply_binding(token: contextvars.Token) -> None:
    """:func:`enter_reply_binding` の札を返して、前の状態に戻す。"""
    _ACTIVE_BINDING.reset(token)


@contextlib.contextmanager
def reply_binding_scope(binding: ReplyModelBinding) -> Iterator[ReplyModelBinding]:
    """返事の間、同じスレッドで呼ばれる処理にも ``binding`` が届くようにする。"""
    token = enter_reply_binding(binding)
    try:
        yield binding
    finally:
        exit_reply_binding(token)


__all__ = [
    "MODEL_SETTINGS_LOCK",
    "PersonaModelRow",
    "ReapplyResult",
    "RejectedModelSetting",
    "ReplyModelBinding",
    "SOURCE_BUILTIN",
    "SOURCE_GLOBAL",
    "SOURCE_OVERRIDE",
    "SOURCE_PERSONA",
    "SpeakingModelChoice",
    "TIER_LIGHTWEIGHT",
    "TIER_STANDARD",
    "UnswitchedPersona",
    "apply_speaking_model",
    "attach_speaking_model_choice",
    "enter_reply_binding",
    "exit_reply_binding",
    "find_reply_binding",
    "global_default_model_setting",
    "global_lightweight_model_setting",
    "initial_speaking_model",
    "live_model_override",
    "model_role_env_keys",
    "read_persona_model_rows",
    "reapply_after_config_reload",
    "reapply_speaking_models",
    "register_new_persona",
    "rejected_global_model_message",
    "rejected_persona_model_message",
    "reply_binding_scope",
    "reply_unavailable_message",
    "resolve_speaking_model",
    "speaking_model_definition",
    "split_undefined_model_updates",
    "undefined_override_message",
    "override_missing_message",
    "unswitched_persona_message",
]
