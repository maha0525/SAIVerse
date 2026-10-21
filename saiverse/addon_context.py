"""アドオンの案内の提供口の登録簿。

アドオンは manifest の ``context_provider`` で「いまこのペルソナに伝えたい案内」
を返す関数を一つ登録できる (例: Godot 仮想身体の部屋でだけ使える全身エモートの
記法)。登録と解除は :mod:`saiverse.addon_loader` が行い、読み手は head の
``addon_guidance`` セクション (sea/head_pipeline/sections/addon_guidance.py)
だけである。

案内は head に置かれ、部屋の移動・アドオンの有効化と無効化・設定の変更で案内が
増えたり消えたり変わったりしたときは、スペル一覧と同じ差分通知の道で
「[システム通知]」としてペルソナの知覚に届く。2026-10-11 に一度リアルタイム
情報 (毎ターン差し替わる末尾メッセージ) へ載せたが、それでは部屋を出て案内が
消えたことが出来事として残らず、過去に自分が書いた記法だけが履歴に残って使い
続ける恐れがあるため、2026-10-12 に head と差分通知へ移した
(docs/intent/addon_speak_hooks.md)。

提供口の呼び出し契約: ``provider(persona=..., building_id=...) -> str``。

- 読み取り専用で、記憶・知覚・世界の状態に書いてはならない。
- すぐ返すこと。提供口は head の撮り直しと差分検知の中 (他のペルソナとも
  共有している処理のロックの内側) で呼ばれるので、ネットワークや待ちのある
  処理をしてはならない (アドオンのスペルの可否判定と同じ制約)。
- 空文字や文字列でない値は「案内なし」として扱う。
- 例外はその提供口だけ警告ログに残し、他のアドオンの案内には波及させない。

一時的な失敗では前回の結果を使う: 提供口が例外を出したとき、そのペルソナ・
部屋・アドオンの組で最後に成功した結果 (「案内なし」だった結果も含む) が記録されて
いれば、それを今回の結果として使う。失敗を「案内なし」として扱うと、失敗した
回の差分検知で「当てはまらなくなりました」の偽通知が出て、復旧後に「届き
ました」の偽通知がもう一度出る。さらに head の撮り直しだけが失敗すると、head
から案内が消えたのに撮り直しは成功した扱いになり、届けた案内の通知も提示から
下ろされ、復旧しても前回知らせた状態と一致するので通知が出ない — ペルソナは
次の撮り直しまで案内を見失う。記録はプロセス内メモリだけに持ち、
``unregister_provider`` (アドオンの無効化・manifest から提供口が消えた・解決に
失敗した) で消す。同じアドオンの提供口の置き換え (``register_provider``) では
消さない。

使える記録が無い失敗 (再起動の直後など) は、そのアドオンだけ「案内なし」として
扱う (エラーログを残す)。2026-10-12 に一度、この場合は案内を集める処理全体を
「撮れなかった」ことにしたが、一つの壊れた提供口が他のアドオンの通知と、
スペル一覧などを含む通知の提示の節約まで止めた。影響を壊れたアドオンだけに
閉じる方を選び、受け入れる残りは「再起動直後の一時的な失敗で、そのアドオンの
案内に限り『当てはまらなくなりました』→『届きました』が一往復出うる」こと。

呼び出しの最中に解除・置き換えされたアドオンの結果は記録も返却もしない
(解除・置き換えの後の世界に、前の提供口の案内を持ち込まない)。置き換えなら、
記録が残っていればそれを使う。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Optional

LOGGER = logging.getLogger(__name__)
_providers: dict[str, Callable[..., str]] = {}
# (persona_id, building_id, addon_name) -> 最後に成功した提供口の結果
# (正規化済み、"" = 案内なし)。部屋を鍵に含めるのは、部屋限定の案内を別の部屋で
# 失敗したときに使い回さないため (退室の瞬間の失敗で、退室先の head に元の部屋の
# 案内が残り、「当てはまらなくなりました」も届かなくなる)。
_last_results: dict[tuple[str, str, str], str] = {}
# addon_name -> 解除の回数。解除と並行して走った呼び出しが、解除の後に古い
# 結果を記録し直さないための目印。
_generations: dict[str, int] = {}
_lock = threading.Lock()


def register_provider(addon_name: str, provider: Callable[..., str]) -> None:
    """提供口を登録する。同じアドオンの既存の提供口は一度の代入で置き換わる
    (置き換えの途中で登録簿からアドオンが消える瞬間は無い)。前回の結果の記録は
    消さない。"""
    with _lock:
        _providers[addon_name] = provider


def unregister_provider(addon_name: str) -> None:
    """提供口を解除し、そのアドオンの前回の結果の記録をすべて消す。"""
    with _lock:
        _providers.pop(addon_name, None)
        _generations[addon_name] = _generations.get(addon_name, 0) + 1
        for key in [key for key in _last_results if key[2] == addon_name]:
            del _last_results[key]


def _normalize(text: Any) -> str:
    return text.strip() if isinstance(text, str) else ""


def collect_guidance(persona: Any, building_id: Any) -> list[tuple[str, str]]:
    """登録された提供口を addon 名の昇順で呼び、``(addon_name, text)`` の列を返す。

    順序を addon 名で固定するのは、同じ世界なら head に毎回同じ文字列が出る
    ことが prefix キャッシュの前提だから。案内を返さなかった提供口は列に
    含めない。提供口が例外を出したときは、モジュールの docstring の規則どおり
    前回の結果を使い、使える記録が無ければそのアドオンだけ案内なしとする。
    ``persona`` から ``persona_id`` が取れないとき・部屋が分からないときは結果を
    記録しない (失敗時に使える前回の結果も無い)。記録はペルソナと部屋の組ごとで、
    別の部屋の記録は使わない。呼び出しの最中に解除・置き換えされたアドオンの
    結果は記録も返却もしない。
    """
    persona_id: Optional[str] = getattr(persona, "persona_id", None) if persona is not None else None
    if not isinstance(persona_id, str) or not persona_id:
        persona_id = None
    # 記録の鍵の前半。ペルソナか部屋が分からないときは記録しない。
    record_scope: Optional[tuple[str, str]] = (
        (persona_id, str(building_id))
        if persona_id is not None and building_id is not None else None
    )
    with _lock:
        providers = [
            (name, provider, _generations.get(name, 0))
            for name, provider in sorted(_providers.items())
        ]
    guidance: list[tuple[str, str]] = []
    for name, provider, generation in providers:
        failed = False
        text = ""
        try:
            text = _normalize(provider(persona=persona, building_id=building_id))
        except Exception:
            failed = True
            LOGGER.warning("addon context provider failed addon=%s", name, exc_info=True)
        with _lock:
            if _generations.get(name, 0) != generation or name not in _providers:
                # 呼び出しの最中に解除された — 解除後の世界に持ち込まない。
                continue
            current = _providers[name] is provider
            if current and not failed and record_scope is not None:
                _last_results[(*record_scope, name)] = text
            previous = (
                _last_results.get((*record_scope, name)) if record_scope is not None else None
            )
        if not current or failed:
            # 置き換えの最中の古い提供口の結果か、失敗。どちらも今回の結果は
            # 使わず、記録があればそれを使う。
            if previous is None:
                if failed:
                    LOGGER.error(
                        "addon context provider failed without a usable last result; "
                        "treating as no guidance for this addon only addon=%s persona=%s",
                        name, persona_id,
                    )
                continue
            if failed:
                LOGGER.info(
                    "addon context provider: using the last successful result "
                    "addon=%s persona=%s has_guidance=%s",
                    name, persona_id, bool(previous),
                )
            text = previous
        if text:
            guidance.append((name, text))
    return guidance


def addon_display_name(addon_name: str) -> str:
    """``expansion_data/<addon_name>/addon.json`` の表示名を日本語優先で返す。

    ``display_name`` は文字列のことも ``{"ja": ..., "en": ...}`` の辞書のことも
    ある (Godot 仮想身体アドオンは辞書)。解決はアドオン管理画面と同じ
    :func:`saiverse.i18n_utils.resolve_i18n_text` に任せ、manifest が読めない・
    表示名が無いときは addon 名をそのまま返す。
    """
    try:
        from saiverse.data_paths import EXPANSION_DATA_DIR
        from saiverse.i18n_utils import resolve_i18n_text

        manifest_path = EXPANSION_DATA_DIR / addon_name / "addon.json"
        with open(manifest_path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return addon_name
        display_en = raw.get("display_name_en")
        return resolve_i18n_text(
            raw.get("display_name"),
            target_lang="ja",
            alt_en=display_en if isinstance(display_en, str) else None,
            default=addon_name,
        ) or addon_name
    except FileNotFoundError:
        return addon_name
    except Exception:
        LOGGER.debug(
            "addon_context: failed to read display_name for addon=%s",
            addon_name, exc_info=True,
        )
        return addon_name
