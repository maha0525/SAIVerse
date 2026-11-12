"""モデルを選ぶ欄の並べ方を決める一か所 (docs/intent/model_picker.md)。

画面はモデルの一覧 (``GET /api/config/models``) を受け取った順番のまま表示し、
画面ごとの並べ替えはしない。どのボタンに入るか・最初から出すか・どの順に並ぶかは、
すべてこのモジュールが決める。

モデル定義とプロバイダ定義は、呼ばれるたびに ``model_configs.MODEL_CONFIGS`` と
``provider_configs.get_provider`` から読む。読み直し (reload) のあとも正しい値を
返すため、起動時の値をこのモジュールに抱え込まない。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from . import model_configs, provider_configs
from .data_paths import (
    LAYER_BUILTIN,
    LAYER_USER_DATA,
    PROVIDERS_DIR,
    iter_file_candidates_with_layer,
)

#: 選ぶ欄での置き場所を決めるプロバイダ定義の項目。
_PLACEMENT_FIELDS = ("picker_group", "current_models")

#: 利用者が自分で作ったプロバイダと、手元で動かすサーバー用の標準プロバイダを
#: まとめるボタンの名前 (決まったこと 8)。
CUSTOM_GROUP = "custom"
CUSTOM_GROUP_LABEL = "ローカル・カスタム"

#: 反射判断専用のプロバイダ (型付きの質問に確率で答えるだけで、文章を書けない) を
#: まとめるボタンの名前。どれもモデルが 1 つずつなので、1 つのボタンに集める。
#: 会話用の欄には、画面の側が reflex_only のモデルを渡さないので出ない。
REFLEX_GROUP = "reflex"
REFLEX_GROUP_LABEL = "反射判断専用"

#: Gemini は有料と無料を別のボタンにする (決まったこと 2)。分ける目印はモデル定義の
#: ``prefer_paid``。
GEMINI_PROVIDER_ID = "gemini"
GEMINI_PAID_GROUP = "gemini_paid"
GEMINI_FREE_GROUP = "gemini_free"
_FIXED_GROUP_LABELS = {
    GEMINI_PAID_GROUP: "Gemini (有料)",
    GEMINI_FREE_GROUP: "Gemini (無料)",
    CUSTOM_GROUP: CUSTOM_GROUP_LABEL,
    REFLEX_GROUP: REFLEX_GROUP_LABEL,
}

#: ボタンの並び順。ここに無いグループは名前の順でこの後ろに並び、custom は最後。
FIXED_GROUP_ORDER: tuple[str, ...] = (
    REFLEX_GROUP,
    "anthropic",
    "openai",
    "openai_codex",
    GEMINI_PAID_GROUP,
    GEMINI_FREE_GROUP,
    "xai",
    "openrouter",
    "nvidia_nim",
)


@dataclass(frozen=True)
class PickerPlacement:
    """1 つのモデルが選ぶ欄のどこに、どう出るか。"""

    picker_group: str
    picker_group_label: str
    picker_group_order: int
    picker_heading: str | None
    is_current: bool
    # 同じ picker_group の中での並び順を決める値。小さいほど上に出る。
    sort_key: tuple[Any, ...]

    def as_fields(self) -> dict[str, Any]:
        """API の応答に載せる 5 項目だけを返す (sort_key は載せない)。"""
        return {
            "picker_group": self.picker_group,
            "picker_group_label": self.picker_group_label,
            "picker_group_order": self.picker_group_order,
            "picker_heading": self.picker_heading,
            "is_current": self.is_current,
        }


def _provider_id_of(config: Mapping[str, Any]) -> str | None:
    """モデル定義が属するプロバイダの id。provider_ref が無い古い形は provider の値。"""
    provider_id = config.get("provider_ref") or config.get("provider")
    return provider_id if isinstance(provider_id, str) and provider_id else None


def _current_models_of(provider: Mapping[str, Any] | None) -> list[str] | None:
    """プロバイダ定義の current_models。持たないとき (または形が違うとき) は None。"""
    if not provider:
        return None
    listed = provider.get("current_models")
    if not isinstance(listed, list):
        return None
    return [name for name in listed if isinstance(name, str)]


def _shipped_placement_fields() -> dict[str, dict[str, Any]]:
    """標準・拡張パックのプロバイダ定義が持つ、置き場所の項目 (id ごと)。

    利用者が標準のプロバイダを画面から編集すると、その定義は user_data の層の
    上書きになる。上書きされても、どのボタンに入るか・最初から出すモデルは、
    配られている定義のとおりにする。上書きの中の ``current_models`` は編集した時点の
    写しで、標準のリストを更新しても古いまま残るため、こちらは見ない。
    """
    shipped: dict[str, dict[str, Any]] = {}
    for config_file, layer in iter_file_candidates_with_layer(PROVIDERS_DIR, "*.json"):
        if layer == LAYER_USER_DATA:
            continue
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError):
            continue
        if not isinstance(data, dict):
            continue
        provider_id = data.get("id", config_file.stem)
        if isinstance(provider_id, str) and provider_id not in shipped:
            shipped[provider_id] = {k: data[k] for k in _PLACEMENT_FIELDS if k in data}
    return shipped


def _placement_source(
    provider_id: str | None,
    provider: Mapping[str, Any] | None,
    shipped: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[Mapping[str, Any] | None, bool]:
    """置き場所の項目を読む先と、利用者が自分で作ったプロバイダかどうかを返す。"""
    if not provider or provider.get("source") != LAYER_USER_DATA:
        return provider, False
    if shipped is not None and provider_id in shipped:
        return shipped[provider_id], False
    return provider, True


def _group_of(
    config: Mapping[str, Any],
    provider_id: str | None,
    placement: Mapping[str, Any] | None,
    user_made: bool,
) -> tuple[str, bool]:
    """どのボタンに入るかと、そのボタンが複数のプロバイダをまとめるものかを返す。"""
    if placement:
        declared = placement.get("picker_group")
        if isinstance(declared, str) and declared:
            return declared, True
    if user_made:
        return CUSTOM_GROUP, True
    if provider_id == GEMINI_PROVIDER_ID:
        return (GEMINI_PAID_GROUP if config.get("prefer_paid") else GEMINI_FREE_GROUP), False
    if provider_id:
        return provider_id, False
    return CUSTOM_GROUP, True


def _provider_display_name(provider_id: str | None, provider: Mapping[str, Any] | None) -> str:
    if provider:
        name = provider.get("display_name")
        if isinstance(name, str) and name:
            return name
    return provider_id or ""


def _group_order_table(groups: set[str]) -> dict[str, int]:
    """ボタンの並び順の番号。固定の順のグループ → それ以外を名前の順 → custom。"""
    order: dict[str, int] = {group: index for index, group in enumerate(FIXED_GROUP_ORDER)}
    others = sorted(
        (g for g in groups if g not in order and g != CUSTOM_GROUP),
        key=lambda g: (g.casefold(), g),
    )
    for group in others:
        order[group] = len(order)
    order[CUSTOM_GROUP] = len(order)
    return order


def compute_placements() -> dict[str, PickerPlacement]:
    """読み込まれている全モデルについて、選ぶ欄での置き場所を返す。

    ボタンの並び順の番号は、一覧に出すかどうかに関係なく全モデルのグループから
    決める。API キーの有無で番号が変わらないようにするため。
    """
    configs = model_configs.MODEL_CONFIGS
    drafts: dict[str, dict[str, Any]] = {}
    # 配られている定義のファイルを読むのは、user_data の層のプロバイダがあるときだけ。
    shipped: dict[str, dict[str, Any]] | None = None
    for key, config in configs.items():
        provider_id = _provider_id_of(config)
        provider = provider_configs.get_provider(provider_id) if provider_id else None
        if shipped is None and provider and provider.get("source") == LAYER_USER_DATA:
            shipped = _shipped_placement_fields()
        placement, user_made = _placement_source(provider_id, provider, shipped)
        group, shared = _group_of(config, provider_id, placement, user_made)
        provider_name = _provider_display_name(provider_id, provider)
        listed = _current_models_of(placement)

        if config.get("source") != LAYER_BUILTIN:
            # 利用者が追加・上書きしたモデルと、拡張パックのモデルは畳まない (決まったこと 6)。
            is_current = True
        elif listed is None:
            # リストを持たないプロバイダは全部を最初から出す (決まったこと 7)。
            is_current = True
        else:
            is_current = key in listed

        display_name = model_configs.get_model_display_name(key)
        if listed is not None and key in listed:
            # リストの順番が表示の順番になる (決まったこと 5)。
            position: tuple[Any, ...] = (0, listed.index(key))
        else:
            position = (1, 0)
        name_key = (str(display_name).casefold(), key)

        heading = provider_name if shared else None
        if heading is not None:
            # 複数のプロバイダをまとめるボタンの中は、プロバイダの名前の見出しで分け、
            # 見出しの中で同じ規則で並べる。
            sort_key: tuple[Any, ...] = (heading.casefold(), heading, *position, *name_key)
        else:
            sort_key = (*position, *name_key)

        if group in _FIXED_GROUP_LABELS:
            label = _FIXED_GROUP_LABELS[group]
        else:
            label = provider_name or group

        drafts[key] = {
            "picker_group": group,
            "picker_group_label": label,
            "picker_heading": heading,
            "is_current": is_current,
            "sort_key": sort_key,
        }

    order = _group_order_table({draft["picker_group"] for draft in drafts.values()})
    return {
        key: PickerPlacement(picker_group_order=order[draft["picker_group"]], **draft)
        for key, draft in drafts.items()
    }


def sort_model_keys(keys: list[str], placements: Mapping[str, PickerPlacement]) -> list[str]:
    """ボタンの並び順 → グループ内の並び順で、モデルの設定キーを並べ替える。"""
    def key_of(model_key: str) -> tuple[Any, ...]:
        placement = placements[model_key]
        return (placement.picker_group_order, placement.picker_group, placement.sort_key)

    return sorted(keys, key=key_of)


__all__ = [
    "CUSTOM_GROUP",
    "CUSTOM_GROUP_LABEL",
    "FIXED_GROUP_ORDER",
    "GEMINI_FREE_GROUP",
    "GEMINI_PAID_GROUP",
    "PickerPlacement",
    "REFLEX_GROUP",
    "REFLEX_GROUP_LABEL",
    "compute_placements",
    "sort_model_keys",
]
