import json
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Mapping

LOGGER = logging.getLogger(__name__)

# Legacy directory-based configs
LEGACY_MODELS_DIR = Path("models")


# Map provider protocol to the legacy 'provider' field name used by factory.py.
# This lets provider_ref resolution emit a config that the existing factory
# code can consume without changes.
_PROTOCOL_TO_LEGACY_PROVIDER = {
    "openai_compat": "openai",
    "ollama_compat": "ollama",
    "anthropic_native": "anthropic",
    "gemini_native": "gemini",
    "xai_native": "xai",
    "nvidia_nim": "nvidia_nim",
    "openai_codex": "openai_codex",
}

# Fields on the model config that can be inherited from the provider when
# unset on the model. Tuple is (model_field, provider_field).
_INHERITABLE_FIELDS = [
    ("base_url", "base_url"),
    ("api_key_env", "api_key_env"),
    ("api_key_env_alternates", "api_key_env_alternates"),
    ("api_key_required", "api_key_required"),
    ("convert_system_to_user", "default_convert_system_to_user"),
    ("max_image_bytes", "default_max_image_bytes"),
    ("supports_images", "default_supports_images"),
    ("request_kwargs", "default_request_kwargs"),
    ("default_headers", "default_headers"),
    ("llama_server_binary", "llama_server_binary"),
]


def _resolve_provider_ref(config: Dict) -> Dict:
    """If config has provider_ref, inherit fields from the referenced provider.

    Direct fields on the model config always take priority. Provider fields
    are used only when the corresponding model field is missing or None.

    If provider_ref points to an unknown provider, logs a warning and returns
    the original config (factory.py will fail later if required fields are
    missing — this surfaces broken refs at runtime rather than hiding them).

    Returns:
        Resolved config dict (a shallow copy if changes were made, otherwise
        the original dict).
    """
    provider_ref = config.get("provider_ref")
    if not provider_ref:
        return config

    # Lazy import to avoid circular dependency with provider_configs
    from .provider_configs import get_provider

    provider = get_provider(provider_ref)
    if provider is None:
        LOGGER.warning(
            "Model references unknown provider_ref=%r; "
            "config will be used as-is (factory may fail if required fields are missing)",
            provider_ref,
        )
        return config

    resolved = dict(config)

    # Map protocol -> legacy provider field for factory.py compatibility
    protocol = provider.get("protocol")
    if protocol:
        if "protocol" not in resolved:
            resolved["protocol"] = protocol
        if "provider" not in resolved:
            resolved["provider"] = _PROTOCOL_TO_LEGACY_PROVIDER.get(protocol, protocol)

    # Inherit provider defaults for fields not set on the model
    for model_field, provider_field in _INHERITABLE_FIELDS:
        if resolved.get(model_field) is None and provider_field in provider:
            resolved[model_field] = provider[provider_field]

    return resolved


def load_configs() -> Dict[str, Dict]:
    """Load model configurations from user_data and builtin_data directories.
    
    Priority order:
    1. user_data/models/ (highest priority)
    2. builtin_data/models/
    3. models/ (legacy, for backwards compatibility)
    """
    from .data_paths import iter_files_with_layer, LAYER_USER_DATA, MODELS_DIR

    configs: Dict[str, Dict] = {}
    seen_keys: set[str] = set()

    # Load from user_data and builtin_data (the iterator handles priority and
    # reports which root each file came from)
    for config_file, layer in iter_files_with_layer(MODELS_DIR, "*.json"):
        try:
            config_data = json.loads(config_file.read_text(encoding="utf-8"))

            # Extract model ID from config (required field for API calls)
            model_id = config_data.get("model")
            if not model_id:
                LOGGER.warning("Model config %s missing 'model' field, skipping", config_file.name)
                continue

            # Which layer declared this model decides what credentials it may
            # name (see saiverse/provider_security.py). Taken from the root the
            # loader walked and written unconditionally, so a definition cannot
            # claim a layer it was not loaded from.
            config_data.pop("source", None)

            # Use filename (without extension) as config key
            config_key = config_file.stem
            if config_key not in seen_keys:
                resolved = _resolve_provider_ref(config_data)
                resolved["source"] = layer
                configs[config_key] = resolved
                seen_keys.add(config_key)
                LOGGER.debug("Loaded model config: %s (model=%s) from %s", config_key, model_id, config_file)
        except Exception as exc:
            LOGGER.warning("Failed to load model config from %s: %s", config_file.name, exc)

    # Fallback to legacy models/ directory if no configs loaded yet
    if not configs and LEGACY_MODELS_DIR.exists() and LEGACY_MODELS_DIR.is_dir():
        for config_file in sorted(LEGACY_MODELS_DIR.glob("*.json")):
            try:
                config_data = json.loads(config_file.read_text(encoding="utf-8"))
                model_id = config_data.get("model")
                if not model_id:
                    continue
                config_key = config_file.stem
                if config_key not in seen_keys:
                    config_data.pop("source", None)
                    resolved = _resolve_provider_ref(config_data)
                    # The legacy in-repo models/ directory predates the three
                    # layers; it is the owner's own checkout, so treat it as such.
                    resolved["source"] = LAYER_USER_DATA
                    configs[config_key] = resolved
                    seen_keys.add(config_key)
            except Exception as exc:
                LOGGER.warning("Failed to load model config from %s: %s", config_file.name, exc)

    LOGGER.info("Loaded %d model configurations", len(configs))
    return configs


MODEL_CONFIGS = load_configs()


def reload_configs() -> Dict[str, Dict]:
    """Reload model configurations from disk and update the global cache.

    Call this after adding, editing, or removing model JSON files
    to pick up changes without restarting the server.
    """
    global MODEL_CONFIGS
    MODEL_CONFIGS = load_configs()
    LOGGER.info("Model configurations reloaded: %d models", len(MODEL_CONFIGS))
    return MODEL_CONFIGS


def get_model_provider(model: str) -> str:
    config = MODEL_CONFIGS.get(model)
    if config is None:
        raise ValueError(
            f"Model config not found: '{model}'. "
            f"Check that a matching JSON file exists in builtin_data/models/ or user_data/models/."
        )
    return config.get("provider", "ollama")


def get_provider_for_model(model: str) -> str | None:
    """Get the effective provider id for a model.

    Returns provider_ref if set, otherwise the legacy 'provider' field value.
    Returns None if neither is configured. Used by the UI to display which
    provider a model belongs to.
    """
    config = MODEL_CONFIGS.get(model, {})
    return config.get("provider_ref") or config.get("provider")


def get_context_length(model: str) -> int:
    config = MODEL_CONFIGS.get(model)
    if config is None:
        raise ValueError(
            f"Model config not found: '{model}'. "
            f"Check that a matching JSON file exists in builtin_data/models/ or user_data/models/."
        )
    return int(config.get("context_length", 120000))


#: Metabolism の二水位 (文字数) の組み込み既定。
#: docs/intent/chronicle_eviction.md §4 — 全モデル一律。旧「モデルごとに
#: バラバラなメッセージ数」は切り分けの混乱の元だったため単位ごと統一した。
#:
#: - target (残す量): Metabolism で軽くする到達点 = 保護範囲。anchor 未確立時の
#:   初期読み込み量もこれを流用する。
#: - high (上限): 提示コンテキストがこれを超えたら Metabolism 発火。
#:
#: 差分の意味: high - target = 一回で削る量。
#:
#: 2026-07-30 に high 12万 → 20万・target 6万 → 10万 (まはー裁定)。12万では実運用の
#: 会話が早々に頭打ちになっていた。一回で削る量は 6万 → 10万字。
#: 2026-09-04 に target 10万 → 4万・high 20万 → 12万 (まはー裁定)。残す量の主語が
#: 会話の行だけになった (知覚ブロックを数えない) ため、旧既定は大きすぎた。
#: 4万字は日本語の会話で百数十往復ほど。差 8万は、知覚の上限を差し引いても
#: 畳みの発火を「たまに・まとめて」に保てる幅。
#:
#: 旧三水位の低水位 (``metabolism_low_chars``、組み込み既定 4万) は 2026-09-04 に
#: 廃止 — 現役の役割が「anchor 未確立時の初期読み込み量」だけに縮んでおり、
#: 発話直前の最終防衛ライン (arasuji_levels.md §15-5) が窓を残す量まで埋め直す
#: ため効く場面が実質無かった (docs/issues/
#: watermarks_unsatisfiable_when_perception_is_large.md 裁定 5)。モデル JSON や
#: 全体設定に残っているキーは黙って無視される (エラーにしない)。
BUILTIN_METABOLISM_TARGET_CHARS = 40_000
BUILTIN_METABOLISM_HIGH_CHARS = 120_000

#: 知覚の提示上限の二水位 (文字数) の組み込み既定。
#: docs/intent/perception_buffer.md §10.9 (2026-09-04 まはー裁定)。
#:
#: Metabolism の水位が束ねるのは**提示の合計**で、その内訳のうち会話の行は畳みで
#: 減らせるが、知覚ブロック (部屋の様子・通知) は畳みの対象外なので、知覚が多い
#: 環境では「残す量まで畳んでも合計が上限を下回らない」状態になる
#: (docs/issues/watermarks_unsatisfiable_when_perception_is_large.md)。そこで
#: 知覚の合計にも自分の二水位を持たせる:
#:
#: - high (上の水位): 提示中の知覚ブロックの合計がこれを超えたら下ろしが起きる。
#: - target (下の水位): 一度の下ろしで**ここまでまとめて**下ろす到達点。
#:
#: 一個ずつではなく「上を超えたら下まで」なのは、下ろすたびに提示の前方が
#: 書き換わるとプロンプトキャッシュがほぼ毎ターン定価の読み直しになるから
#: (窓の並びが変わるのは「たまに・まとめて」— intent cached_head_architecture)。
#:
#: 既定の 6万 / 4万は、Metabolism 側の既定 (残す量 4万 / 上限 12万) と組んで
#: 「会話を残す量まで畳めば合計が上限を下回る」(12万 − 4万 = 8万 > 6万 + 余裕 1万) が
#: 成り立つ幅。数字はまはーの製品判断で見直しうる。
#:
#: 読み経路は Metabolism の水位と同じ三層 (モデル定義 > 全体設定 > 組み込み既定)。
BUILTIN_PERCEPTION_TARGET_CHARS = 20_000  # 2026-09-05 まはー裁定: 幅 (high−target) が省略の頻度 = キャッシュの割れ頻度を決める。幅 2 万は会話の整理 (幅 8 万) の 4 倍の頻度で割るため 4 万へ広げた
BUILTIN_PERCEPTION_HIGH_CHARS = 60_000

#: 保存時検査「整理を始める量 − 残す量 > 知覚の上限 + 余裕」の**余裕**の分。
#: docs/issues/watermarks_unsatisfiable_when_perception_is_large.md 裁定 4。
#:
#: 等号ぎりぎり (差 = 知覚の上限) では成立しない — 畳んだ後の会話は端数
#: (材料 U 未満の畳み残し) と関節寄せで「残す量ちょうど」には収まらず、知覚も
#: 下の水位まで下ろした後に新着が積まれる。この余裕はその取りこぼしの分。
#:
#: 値はまはーの裁定待ちの叩き台 (2026-09-05 時点)。ここ一箇所だけを直せば
#: 保存時検査 (api/routes/config.py) と UI の事前検査が同時に追随する。
WATERMARK_HEADROOM_CHARS = 10_000

#: モデル定義で上書きするときのキー名 (順に 下の水位 / 上の水位)。
PERCEPTION_WATERMARK_KEYS: tuple[str, str] = (
    "perception_target_chars", "perception_high_chars",
)
BUILTIN_PERCEPTION_DEFAULTS: dict[str, int] = {
    "perception_target_chars": BUILTIN_PERCEPTION_TARGET_CHARS,
    "perception_high_chars": BUILTIN_PERCEPTION_HIGH_CHARS,
}

#: 水位のキー名 → 組み込み既定。全体設定 / API / UI が同じ組を回すための表。
METABOLISM_WATERMARK_KEYS: tuple[str, str] = (
    "metabolism_target_chars", "metabolism_high_chars",
)
BUILTIN_METABOLISM_DEFAULTS: dict[str, int] = {
    "metabolism_target_chars": BUILTIN_METABOLISM_TARGET_CHARS,
    "metabolism_high_chars": BUILTIN_METABOLISM_HIGH_CHARS,
}

#: 二族 (Metabolism / 知覚) を合わせた水位の全キー。全体設定・保存時検査・UI は
#: この四つを一組として扱う — 保存時検査 (整理を始める量 − 残す量 > 知覚の上限
#: + 余裕) が二族をまたぐので、既定の層を族ごとに分けると「片方だけ新しい組」を
#: 読む瞬間が生まれる。
ALL_WATERMARK_KEYS: tuple[str, ...] = METABOLISM_WATERMARK_KEYS + PERCEPTION_WATERMARK_KEYS
BUILTIN_WATERMARK_DEFAULTS: dict[str, int] = {
    **BUILTIN_METABOLISM_DEFAULTS,
    **BUILTIN_PERCEPTION_DEFAULTS,
}

#: 全体設定の水位既定 (2026-09-03、2026-09-05 に知覚の二水位も同居)。三層の真ん中:
#: 組み込み既定 < **全体設定** < モデル定義。キー無し / None = 未設定
#: (組み込み既定に落ちる)。
#:
#: 真実の置き場は DB (user_settings.{METABOLISM,PERCEPTION}_*_CHARS)。このモジュールは
#: DB を触らない約束なので、起動時 (saiverse_manager) と API の保存成功時に
#: ``set_global_watermark_defaults`` で写してもらう。
#:
#: **不変の写像を丸ごと差し替える** (clear → 逐次代入ではない)。読み手は
#: ``_current_global_defaults()`` で一枚の写像を取り、その一枚から水位を解く
#: (``resolve_metabolism_watermarks`` / ``resolve_perception_watermarks``) ので、
#: 差し替えの途中で「target は新・high は旧」の混ざった組を観測しない
#: (Codex 指摘 2026-09-03)。外から読むときは ``get_global_watermark_defaults()``
#: を使う (この変数名は private)。
#:
#: 2026-07-30 に撤去した揮発性のグローバル上書き (会話ごとの画面に置かれ、効く範囲が
#: 不明瞭だった) とは別物 — こちらは永続の**既定**で、モデル定義に数値があれば
#: そちらが勝つ (docs/concepts/metabolism.md)。
_GLOBAL_WATERMARK_DEFAULTS: Mapping[str, int | None] = MappingProxyType(
    {key: None for key in ALL_WATERMARK_KEYS}
)


def _current_global_defaults() -> Mapping[str, int | None]:
    """全体設定の水位既定の**現在の一枚** (不変)。読み手はこれを一度だけ取る。"""
    return _GLOBAL_WATERMARK_DEFAULTS


def _positive_int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = int(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def set_global_watermark_defaults(values: Mapping[str, int | None]) -> None:
    """全体設定の水位既定を差し替える (四キー以外は無視、正の整数以外は未設定扱い)。

    廃止済みの ``metabolism_low_chars`` が混ざっていても黙って無視する
    (旧 DB / 旧クライアントとのデータ互換)。

    新しい不変写像を作って一回の代入で差し替える — 読み手が持っている古い一枚は
    変化しない。二族 (Metabolism / 知覚) を一枚に収めているので、両方を変える
    保存でも「片方だけ新しい組」を読む瞬間が生まれない。
    """
    global _GLOBAL_WATERMARK_DEFAULTS
    _GLOBAL_WATERMARK_DEFAULTS = MappingProxyType({
        key: _positive_int_or_none(values.get(key)) for key in ALL_WATERMARK_KEYS
    })


def get_global_watermark_defaults() -> dict[str, int | None]:
    """全体設定の水位既定 (未設定は None) を四キーそろえて返す。"""
    current = _current_global_defaults()
    return {key: current.get(key) for key in ALL_WATERMARK_KEYS}


def effective_watermark_defaults_from(
    global_defaults: Mapping[str, int | None],
) -> dict[str, int]:
    """全体既定の一枚を「キー無しのモデルが従う実効既定」(四キー) に均す。

    保存時検査は「その全体既定にしたら既存モデルはどうなるか」を調べるので、
    *提案中の* 全体既定でも同じ均し方ができる必要がある (api/routes/config.py の
    `_models_conflicting_with_defaults`)。
    """
    return {
        key: global_defaults.get(key) or BUILTIN_WATERMARK_DEFAULTS[key]
        for key in ALL_WATERMARK_KEYS
    }


def get_effective_watermark_defaults() -> dict[str, int]:
    """キー無しのモデルが実際に従う既定 = 全体設定があればそれ、無ければ組み込み。

    UI の空欄プレースホルダとモデル保存時の検証 (api/routes/config.py) が使う。
    """
    return effective_watermark_defaults_from(_current_global_defaults())


def compose_watermark(
    config: Mapping[str, Any], key: str, effective_default: int,
) -> int | None:
    """モデル定義一つと既定一つから、水位一つを実行時と同じ規則で決める。

    キーが**無い**なら ``effective_default``。キーが有って ``null`` / 0 以下なら
    None = その水位を持たない (モデル単位のオプトアウト)。数値に読めない値は
    ``effective_default`` に落ちる。全体既定の保存時に「既存モデルの部分上書きと
    矛盾しないか」を調べる側 (api/routes/config.py) もこの関数で組む — 実行時の
    解決とそこが二本に分かれると、検証は通るのに実行時は壊れる事故が起きる。

    規則は Metabolism の水位と知覚の提示上限で共通なので、名前は水位一般で持つ。
    """
    if key not in config:
        return effective_default
    val = config.get(key)
    if val is None:
        return None
    try:
        num = int(val)
    except (TypeError, ValueError):
        return effective_default
    return num if num > 0 else None


def compose_metabolism_watermarks(
    config: Mapping[str, Any], global_defaults: Mapping[str, int | None],
) -> tuple[int | None, int | None]:
    """モデル定義一つと全体既定の一枚から (target, high) を組む。"""
    effective = effective_watermark_defaults_from(global_defaults)
    target, high = (
        compose_watermark(config, key, effective[key])
        for key in METABOLISM_WATERMARK_KEYS
    )
    return target, high


def resolve_metabolism_watermarks(model: str) -> tuple[int | None, int | None]:
    """Metabolism の二水位 (target, high) を**一枚の全体既定**から解決する。

    三層: モデル定義にキーが無いなら全体設定の既定、それも無ければ組み込み既定
    (一律)。キーが有って ``null`` / 0 以下なら None = その水位を持たない (これは
    モデル単位のオプトアウトで、全体設定では表せない)。high が None のとき
    文字数による発火は起きず、``token_triggered`` だけが Metabolism を起こす
    (chronicle_eviction.md §4)。モデル定義に残っている廃止済みの
    ``metabolism_low_chars`` キーは読まない = 黙って無視される。

    全体既定は ``_current_global_defaults()`` を**一度だけ**読む。水位を別々の
    getter で取ると、間に ``set_global_watermark_defaults`` が挟まったとき
    新旧の混ざった組になる — 組で要る呼び手 (sea/session_lifecycle.py)
    はこちらを使う。
    """
    config = MODEL_CONFIGS.get(model, {})
    return compose_metabolism_watermarks(config, _current_global_defaults())


def compose_all_watermarks(
    config: Mapping[str, Any], global_defaults: Mapping[str, int | None],
) -> dict[str, int | None]:
    """四水位を**素のまま** (実行時の救済を掛けずに) 組む。保存時検査の入力。

    実行時の解決 (``compose_perception_watermarks``) は、下の水位が潰れていたら
    既定へ戻し、上下が逆転していたら下を上へ寄せる — 走らせるための縮退であって、
    保存の入口で「その設定は壊れている」と教える責務の代わりではない。だから
    検査はこの素の組で行う (docs/issues/
    watermarks_unsatisfiable_when_perception_is_large.md 裁定 4)。
    """
    effective = effective_watermark_defaults_from(global_defaults)
    return {
        key: compose_watermark(config, key, effective[key])
        for key in ALL_WATERMARK_KEYS
    }


def compose_perception_watermarks(
    config: Mapping[str, Any], global_defaults: Mapping[str, int | None],
) -> tuple[int, int | None]:
    """モデル定義一つと全体既定の一枚から知覚の (下の水位, 上の水位) を組む。

    キーが有って ``null`` / 0 以下なら上の水位は None = **下ろしを持たない**
    (モデル単位のオプトアウト。合計は伸びるに任せる)。下の水位が None に潰れた
    場合は実効既定へ戻す — 下ろす到達点が無いと「どこまで下ろすか」が決まらない
    ので、ここにオプトアウトは無い。上下が逆転している設定は、下の水位を上の
    水位まで下げて受ける (下ろした直後にまた超過している状態を作らない)。
    """
    effective = effective_watermark_defaults_from(global_defaults)
    target = compose_watermark(
        config, "perception_target_chars", effective["perception_target_chars"],
    )
    high = compose_watermark(
        config, "perception_high_chars", effective["perception_high_chars"],
    )
    if target is None:
        target = effective["perception_target_chars"]
    if high is not None and target > high:
        target = high
    return int(target), (int(high) if high is not None else None)


def resolve_perception_watermarks(model: str) -> tuple[int, int | None]:
    """知覚の提示上限の二水位 (下の水位, 上の水位) を解決する。

    三層: モデル定義にキーが無いなら全体設定の既定、それも無ければ組み込み既定
    (:data:`BUILTIN_PERCEPTION_DEFAULTS`)。全体既定は
    ``_current_global_defaults()`` を**一度だけ**読む (Metabolism 側と同じ理由 —
    水位ごとに別々に読むと新旧の混ざった組になる)。
    """
    config = MODEL_CONFIGS.get(model, {})
    return compose_perception_watermarks(config, _current_global_defaults())


def _metabolism_chars(model: str, key: str) -> int | None:
    """水位一つを解決する (単発の getter 用。組で要るなら resolve_… を使う)。"""
    config = MODEL_CONFIGS.get(model, {})
    effective = effective_watermark_defaults_from(_current_global_defaults())
    return compose_watermark(config, key, effective[key])


def get_metabolism_target_chars(model: str) -> int | None:
    """目標水位 = Metabolism 後に到達したい文字数 (``metabolism_target_chars``)。"""
    return _metabolism_chars(model, "metabolism_target_chars")


def get_metabolism_high_chars(model: str) -> int | None:
    """高水位 = Metabolism 発火の文字数 (``metabolism_high_chars``)。

    None は「文字数では発火しない」(token_triggered のみ) を意味する。
    """
    return _metabolism_chars(model, "metabolism_high_chars")


def get_metabolism_token_threshold(model: str) -> int | None:
    """Get the token threshold that triggers metabolism.

    When usage.input_tokens exceeds this value after a successful LLM call,
    metabolism is triggered. Returns None if not configured (time-based fallback).
    """
    config = MODEL_CONFIGS.get(model, {})
    val = config.get("metabolism_token_threshold")
    if val is not None:
        return int(val)
    return None


def get_max_image_embeds(model: str) -> int | None:
    """Get the maximum number of image embeds for a model.

    Returns None if not configured (no limit on image embeds).
    """
    config = MODEL_CONFIGS.get(model, {})
    val = config.get("max_image_embeds")
    if val is not None:
        return int(val)
    return None


def get_model_display_name(model: str) -> str:
    """Get display name for a model, falling back to model ID if not set."""
    config = MODEL_CONFIGS.get(model, {})
    return config.get("display_name", model)


def get_model_choices() -> list[str]:
    """Get list of available model IDs."""
    return list(MODEL_CONFIGS.keys())


def get_model_choices_with_display_names() -> list[tuple[str, str]]:
    """Get list of (model_id, display_name) tuples for UI dropdowns."""
    return [(model_id, get_model_display_name(model_id)) for model_id in get_model_choices()]


def get_model_config(model: str) -> Dict:
    return MODEL_CONFIGS.get(model, {})


def model_supports_images(model: str) -> bool:
    config = get_model_config(model)
    return bool(config.get("supports_images"))


def model_supports_audio(model: str) -> bool:
    config = get_model_config(model)
    return bool(config.get("supports_audio"))


def model_supports_video(model: str) -> bool:
    config = get_model_config(model)
    return bool(config.get("supports_video"))


def get_model_parameters(model: str) -> Dict[str, Dict[str, Any]]:
    config = get_model_config(model)
    params = config.get("parameters")
    if isinstance(params, dict):
        return params
    return {}


def get_model_parameter_defaults(model: str) -> Dict[str, Any]:
    defaults: Dict[str, Any] = {}
    for name, spec in get_model_parameters(model).items():
        if isinstance(spec, dict) and "default" in spec:
            defaults[name] = spec.get("default")
    return defaults


def get_model_system_prompt(model: str) -> str:
    """Get the additional system prompt defined in model config.

    Returns an empty string if not configured.
    """
    config = get_model_config(model)
    return config.get("system_prompt", "") or ""


def get_structured_output_backend(model: str) -> str | None:
    """Get structured output backend for a model (e.g., 'xgrammar', 'outlines')."""
    config = get_model_config(model)
    return config.get("structured_output_backend")


def supports_structured_output(model: str) -> bool:
    """Check if a model supports structured output.

    Returns True by default unless explicitly set to False in model config.
    """
    config = get_model_config(model)
    # Default to True unless explicitly set to False
    return config.get("supports_structured_output", True)


def find_model_config(query: str) -> tuple[str, Dict]:
    """Find model config by model ID or filename.

    Searches in order:
    1. Exact match on config key (filename without .json)
    2. Exact match on config["model"] value (API model name)
    3. Exact filename match from file system
    4. Partial match on model ID suffix (e.g., "qwen3-coder" matches "qwen/qwen3-coder-480b...")

    Args:
        query: Model ID, filename, or partial match

    Returns:
        Tuple of (config_key, config) or ("", {}) if not found
    """
    # 1. Exact match on config key (filename)
    if query in MODEL_CONFIGS:
        return query, MODEL_CONFIGS[query]

    # 2. Exact match on config["model"] value (API model name)
    for config_key, config in MODEL_CONFIGS.items():
        if config.get("model") == query:
            return config_key, config

    # 3. Check exact filename match - load config directly from file
    from .data_paths import get_data_paths, MODELS_DIR

    for models_path in get_data_paths(MODELS_DIR):
        config_file = models_path / f"{query}.json"
        if config_file.exists():
            try:
                config_data = json.loads(config_file.read_text(encoding="utf-8"))
                model_id = config_data.get("model", query)
                # Return the query (filename) as the resolved ID so caller knows which file was used
                # But include the actual model ID in the config for API calls
                return query, config_data
            except Exception:
                LOGGER.warning("Failed to load model config from %s", config_file, exc_info=True)

    # 4. Partial match on model ID (query is suffix or contains)
    for model_id, config in MODEL_CONFIGS.items():
        # Check if query matches the part after "/" (e.g., "qwen3-coder-480b" matches "qwen/qwen3-coder-480b")
        if "/" in model_id:
            suffix = model_id.split("/", 1)[1]
            if query == suffix or suffix.startswith(query):
                return model_id, config

    return "", {}



def get_model_pricing(model: str) -> Dict[str, Any] | None:
    """Get pricing information for a model.

    Uses find_model_config to search by both config key and model ID.

    Returns:
        Dict with keys:
            - input_per_1m_tokens: float (USD per 1M input tokens)
            - output_per_1m_tokens: float (USD per 1M output tokens)
            - cached_input_per_1m_tokens: float (optional cache-read rate)
            - long_context_threshold_tokens: int (optional, exclusive threshold)
            - long_context_*_per_1m_tokens: float (optional rates above threshold)
            - currency: str (e.g., "USD")
        Or None if pricing not configured.
    """
    # First try direct lookup
    config = get_model_config(model)
    pricing = config.get("pricing")
    if isinstance(pricing, dict):
        return pricing

    # Fall back to find_model_config which searches by model ID too
    _, config = find_model_config(model)
    if config:
        pricing = config.get("pricing")
        if isinstance(pricing, dict):
            return pricing

    return None


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_ttl: str = "",
) -> float:
    """Calculate cost in USD for a given token usage.

    Args:
        model: Model ID (config key)
        input_tokens: Number of input tokens (total including cached and cache_write)
        output_tokens: Number of output tokens
        cached_tokens: Number of tokens served FROM cache (cache read, discounted rate)
        cache_write_tokens: Number of tokens written TO cache
        cache_ttl: Cache TTL used ("5m" or "1h"). Affects write cost for Anthropic.

    Returns:
        Cost in USD. Returns 0.0 if pricing not configured (e.g., local models).

    Note:
        Token breakdown for Anthropic:
        - cached_tokens: Read from cache (0.1x rate)
        - cache_write_tokens: Written to cache (1.25x rate for 5m, 2x rate for 1h)
        - remaining: Regular input tokens (1x rate)

        For Gemini/OpenAI (implicit caching):
        - cached_tokens: Read from cache (discounted rate)
        - cache_write_tokens: 0 (no explicit write cost)
    """
    pricing = get_model_pricing(model)
    LOGGER.debug("[DEBUG] calculate_cost: model=%s, pricing=%s", model, pricing)
    if not pricing:
        LOGGER.debug("[DEBUG] No pricing found for model: %s", model)
        return 0.0

    long_context_threshold = pricing.get("long_context_threshold_tokens")
    use_long_context_rates = (
        isinstance(long_context_threshold, int)
        and not isinstance(long_context_threshold, bool)
        and input_tokens > long_context_threshold
    )
    rate_prefix = "long_context_" if use_long_context_rates else ""

    input_rate = pricing.get(
        f"{rate_prefix}input_per_1m_tokens",
        pricing.get("input_per_1m_tokens", 0.0),
    )
    output_rate = pricing.get(
        f"{rate_prefix}output_per_1m_tokens",
        pricing.get("output_per_1m_tokens", 0.0),
    )
    # Cached tokens (read): use explicit cached rate if configured, otherwise same as input rate
    cached_rate = pricing.get(
        f"{rate_prefix}cached_input_per_1m_tokens",
        pricing.get("cached_input_per_1m_tokens", input_rate),
    )
    # Cache write tokens: use TTL-specific rate if available
    if cache_ttl == "1h" and "cache_write_1h_per_1m_tokens" in pricing:
        cache_write_rate = pricing["cache_write_1h_per_1m_tokens"]
    else:
        cache_write_rate = pricing.get(
            f"{rate_prefix}cache_write_per_1m_tokens",
            pricing.get("cache_write_per_1m_tokens", input_rate),
        )

    # Non-cached input tokens (input_tokens includes cached + cache_write, so subtract both)
    non_cached_input = max(0, input_tokens - cached_tokens - cache_write_tokens)

    non_cached_cost = (non_cached_input / 1_000_000) * input_rate
    cached_cost = (cached_tokens / 1_000_000) * cached_rate
    cache_write_cost = (cache_write_tokens / 1_000_000) * cache_write_rate
    output_cost = (output_tokens / 1_000_000) * output_rate

    total = non_cached_cost + cached_cost + cache_write_cost + output_cost
    currency = pricing.get("currency", "USD")
    LOGGER.debug(
        "[DEBUG] Cost calculated: %.6f %s (tier=%s, non_cached_in=%d @ %.4f, cached=%d @ %.4f, cache_write=%d @ %.4f, out=%d @ %.4f)",
        total, currency, "long" if use_long_context_rates else "standard",
        non_cached_input, input_rate, cached_tokens, cached_rate,
        cache_write_tokens, cache_write_rate, output_tokens, output_rate,
    )
    return total


def calculate_cache_storage_cost(model: str, cached_tokens: int, ttl_seconds: int) -> float:
    """Calculate explicit-cache storage cost in USD.

    Gemini explicit cache bills storage as an hourly rate per 1M cached tokens,
    prorated. We adopt a "reserved seat" model: at create time we charge the
    FULL TTL window up front (cached_tokens x rate x ttl_hours). If a delete
    mechanism later frees the cache early, the unused remainder is refunded as a
    negative record. Returns 0.0 when pricing or the storage rate is absent
    (e.g. free-tier models without a pricing block).

    See docs/intent/cache_lifecycle_control.md (storage accounting).
    """
    pricing = get_model_pricing(model)
    if not pricing:
        return 0.0
    rate = pricing.get("cache_storage_per_1m_tokens_per_hour", 0.0)
    if rate <= 0 or cached_tokens <= 0 or ttl_seconds <= 0:
        return 0.0
    return (cached_tokens / 1_000_000) * rate * (ttl_seconds / 3600.0)


def _get_required_env_vars(model: str) -> list[str]:
    """Return the environment variable names required for a model's API key.

    Returns an empty list for local models (ollama, llama_cpp) that need no key.
    For models with multiple possible keys (e.g. Gemini), returns all alternatives
    — the model is available if ANY of them is set.
    """
    config = MODEL_CONFIGS.get(model, {})
    provider = config.get("provider", "")

    # Providers that declare no authentication (local servers such as LM Studio
    # or llama.cpp, which speak the OpenAI protocol but accept any key).
    if config.get("api_key_required") is False:
        return []

    # Local models need no API key
    if provider in ("ollama", "llama_cpp"):
        return []

    # Explicit api_key_env in config takes priority. Alternates (inherited from
    # the provider via provider_ref, e.g. Gemini's free-tier key) are additional
    # accepted names — the model is available if ANY of them is set, so they
    # must be returned alongside the primary name rather than replaced by it.
    api_key_env = config.get("api_key_env")
    if api_key_env:
        names = [api_key_env]
        alternates = config.get("api_key_env_alternates")
        if isinstance(alternates, list):
            names.extend(
                alt for alt in alternates
                if isinstance(alt, str) and alt and alt not in names
            )
        return names

    # Provider defaults
    if provider == "anthropic":
        return ["CLAUDE_API_KEY"]
    if provider == "gemini":
        return ["GEMINI_API_KEY", "GEMINI_FREE_API_KEY"]
    if provider in ("openai",):
        return ["OPENAI_API_KEY"]
    if provider == "xai":
        return ["XAI_API_KEY"]

    # Unknown provider — assume available (don't hide by mistake)
    return []


def is_model_available(model: str) -> bool:
    """Check if a model's required API key is configured.

    Returns True if:
    - The model needs no API key (local models), or
    - At least one of the required env vars is set, or
    - The provider is unknown (don't hide by mistake).
    """
    env_vars = _get_required_env_vars(model)
    if not env_vars:
        return True
    return any(os.environ.get(var) for var in env_vars)


def is_local_model(model: str) -> bool:
    """Check if a model is a local model (Ollama or llama.cpp).

    Local models have zero API cost.
    Returns False if the model config is not found.
    """
    config = MODEL_CONFIGS.get(model)
    if config is None:
        return False
    return config.get("provider") in ("ollama", "llama_cpp")


def get_rate_limit_config(model: str) -> Dict[str, Any] | None:
    """Get rate limit configuration for a model.

    Returns:
        Dict with keys:
            - rpd: int (requests per day limit)
            - reset_timezone: str (timezone for daily reset, e.g. "America/Los_Angeles")
        Or None if no rate limit configured.
    """
    config = get_model_config(model)
    rate_limit = config.get("rate_limit")
    if isinstance(rate_limit, dict) and rate_limit.get("rpd"):
        return rate_limit
    return None


def get_cache_config(model: str) -> Dict[str, Any]:
    """Get cache configuration for a model.

    Returns:
        Dict with keys:
            - supported: bool (whether model supports caching)
            - default_enabled: bool (default cache state)
            - default_ttl: str (e.g., "5m", "1h")
            - ttl_options: list[str] (available TTL options)
            - type: str ("explicit" or "implicit")
            - min_tokens: int (minimum tokens for caching)
    """
    config = get_model_config(model)
    cache = config.get("cache", {})

    # Determine if caching is supported based on provider
    # Use direct config lookup to avoid ValueError when model config is missing
    provider = config.get("provider")
    default_supported = provider in ("anthropic", "gemini", "openai")

    return {
        "supported": cache.get("supported", default_supported),
        "default_enabled": cache.get("default_enabled", True),
        "default_ttl": cache.get("default_ttl", "5m"),
        "ttl_options": cache.get("ttl_options", ["5m"]),
        "type": cache.get("type", "explicit" if provider == "anthropic" else "implicit"),
        "min_tokens": cache.get("min_tokens", 1024),
    }


def supports_cache(model: str) -> bool:
    """Check if a model supports prompt caching."""
    return get_cache_config(model).get("supported", False)


def get_cache_ttl_options(model: str) -> list:
    """Get available cache TTL options for a model."""
    return get_cache_config(model).get("ttl_options", ["5m"])


def get_cache_write_rate(model: str) -> float:
    """Get cache write cost rate per 1M tokens.

    For Anthropic:
        - 5m TTL: 1.25x input rate
        - 1h TTL: 2x input rate (not yet supported)

    Returns 0.0 if not configured (implicit caching has no write cost).
    """
    pricing = get_model_pricing(model)
    if not pricing:
        return 0.0
    return pricing.get("cache_write_per_1m_tokens", 0.0)
