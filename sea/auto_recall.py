"""自動想起 第0層 (ゾーン C) — 埋め込み検索による「浮かんだ記憶」の末尾注入。

記憶アーキテクチャ v2 (docs/intent/memory_architecture_v2.md) §4 の中核。ペルソナに
「思い出す」という行動を選ばせるのではなく、世界側が現在の会話に関連する記憶を
ローカル埋め込み検索で自動的に浮かび上がらせ、``<system>`` タグ付き user メッセージ
として履歴末尾 (最新入力の直前) に**一時注入**する。

不変条件 (§10):
- **LLM は絶対に呼ばない** (§10-1)。埋め込み検索 + 決定論フィルタのみ。
  §10-1 が認める唯一の例外が「明示的なオプション層としての選別」で、その実装が
  反射判断による選別 (ペルソナごとの「自動想起を強化する」スイッチ、既定 OFF。
  docs/intent/auto_recall_jev_rerank.md と docs/intent/reflex_judgment.md)。
  文章生成は行わず、判断専用モデルに候補の関連度だけを問う。OFF では一切呼ばない。
- 注入は履歴末尾のみ、head 非混入 (§10-2、cached_head_architecture C5 と同じ面)。
- SAIMemory には**永続化しない** (§10-7)。LLM に渡す message 列にだけ足す。

スコープ (§4.6): メインライン (CONVERSATION アスペクト) のみ。呼び出し側で
``aspect_from_pulse_type(pulse_type) == Aspect.CONVERSATION`` を確認してから呼ぶ。

粘着ウィンドウ (§4.3): 一度浮かんだ記憶は類似度がしきい値を割っても N ターン残す。
プロセス内メモリ (``_LEDGERS``、(persona_id, thread_id) キー) で持ち、再起動で
消えて構わない。
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from sea.eviction_plan import CONSUMED_PERCEPTION_KEY, is_injected_perception
from saiverse.references import to_uri

LOGGER = logging.getLogger("saiverse.auto_recall")


# ---------------------------------------------------------------------------
# パラメータ (env で調整可能。既定値は intent doc §4 / §12 準拠)
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        LOGGER.warning("[auto_recall] invalid float for %s=%r; using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        LOGGER.warning("[auto_recall] invalid int for %s=%r; using default %s", name, raw, default)
        return default


# 埋め込みヒットの採用しきい値 (raw cosine similarity)。下回れば注入しない。
#
# 既定 0.86 は air_city_a の実 memory.db で測定して決めた (intent doc §4 は暫定
# 0.78 だったが、E5 (multilingual-e5-small) は query:/passage: プレフィックス方式
# ゆえ cosine レンジが 0.8〜0.9 に圧縮され、無関係クエリでも top ヒットが 0.83〜0.84
# に達する。0.78 では毎ターン全件注入になってしまう)。実測分布:
#   - オントピック: top クラスタ 0.86〜0.89
#   - 無関係/gibberish: top max 0.842
#   - 汎用短文「うん」: top max 0.837
# 0.86 でオントピックの上位ヒットを拾いつつノイズを落とせる。env で調整可。
def get_similarity_threshold() -> float:
    return _env_float("SAIVERSE_AUTO_RECALL_THRESHOLD", 0.86)


# 粘着ウィンドウ: しきい値を割ってから台帳に残すターン数。
def get_sticky_turns() -> int:
    return _env_int("SAIVERSE_AUTO_RECALL_STICKY_TURNS", 3)


# クエリに含める直近メッセージ数 (user/assistant 両ロール)。
#
# rev2: 既定を 4 → 1 に変更。air_city_a の実 memory.db で測定した結果、直近4件
# (210字超) を連結すると会話の主成分 (実装の話など) にクエリベクトルが支配され、
# 発話に含まれる固有名詞 (例:「アイフィ」) が埋没して無関係な記憶が 0.87〜0.91 で
# threshold 0.86 を通過してしまう。クエリを最新発話 1 件だけに絞ると、固有名詞への
# 連想が 0.87〜0.88 で正しく浮上した。env で引き上げは引き続き可能。
def get_query_message_count() -> int:
    return _env_int("SAIVERSE_AUTO_RECALL_QUERY_MESSAGES", 1)


# unified_recall の topk (多めに取って選別で絞る)。
def get_topk() -> int:
    return _env_int("SAIVERSE_AUTO_RECALL_TOPK", 8)


# 添付メディア (画像/音声/動画) の概要を自動想起クエリに使うか (グローバル設定)。
# 既定 OFF。ON 時は chat.py が同期生成した概要 (metadata["images"]/["media"] の
# 各エントリの "summary" キー) を build_query が拾ってクエリに足す。書き込み側
# (api/routes/config.py set_media_recall) が write_env_updates() で os.environ
# にも即時反映するため、他パラメータと同様に env を都度読むだけで再起動なしに
# 反映される。docs/intent/memory_architecture_v2.md §4.1 参照。
def is_media_recall_enabled() -> bool:
    return os.getenv("SAIVERSE_MEDIA_RECALL_ENABLED", "").strip().lower() in ("1", "true", "yes")


# message ソースのヒットに加算するしきい値オフセット (raw cosine similarity)。
#
# 実測: message ソースは長文同士の類似度インフレが起きやすく、無関係な組み合わせ
# でも 0.89〜0.91 のジャンクヒットが出る (chronicle/memopedia/fragment は短めの
# 要約文なのでこのインフレが起きにくい)。message ヒットだけ threshold を底上げして
# ジャンクを落とす。既定 0.02 (= 実効しきい値 0.88)。
def get_message_threshold_offset() -> float:
    return _env_float("SAIVERSE_AUTO_RECALL_MSG_THRESHOLD_OFFSET", 0.02)


# エンティティトリガーの ambient 抑制: 直近ウィンドウ (最新発話を除く) でこの回数
# 以上タイトルが出現していたらスキップする。エア・まはー・SAIVerse のような常在
# エンティティが毎ターン浮かぶのを防ぎ、「珍しい名前ほど連想が走る」性質を出す。
def get_entity_ambient_count() -> int:
    return _env_int("SAIVERSE_AUTO_RECALL_ENTITY_AMBIENT_COUNT", 3)


# 鮮明想起 (§C, vivid recall) のサブ行 (「そのときの会話」) 合計文字数上限。
# CHAR_BUDGET を共有する設計 (§4 の仕様: 鮮明な記憶がひとつ浮かぶと他はぼんやり
# する) なので、これは vivid サブ行そのものの上限にすぎない。
def get_vivid_char_budget() -> int:
    return _env_int("SAIVERSE_AUTO_RECALL_VIVID_CHARS", 400)


# ---------------------------------------------------------------------------
# 反射判断による選別 (ペルソナごとの「自動想起を強化する」スイッチが ON のときだけ)
#
# 埋め込み類似度のしきい値だけでは「意味が近い」しか測れず、「今この場面で浮かぶ
# べきか」を測れない。反射判断 (saiverse/reflex_judgment.py) に候補ごとの関連度
# (Noul 確率) を 1 リクエストで問い、その判定を採否に使う。
# 設計と実験の合否判定は docs/intent/auto_recall_jev_rerank.md、判断層そのものは
# docs/intent/reflex_judgment.md。
#
# 下の 4 つの数字は Jev の実験 (2026-09) で調整して決めた値。かつては env で
# 動かせたが、利用者向けの設定ではなく実験のつまみだったので、載せ替え
# (2026-09-20) でモジュール定数に畳んだ。例外は「何秒まで待つか」だけで、そこは
# 使う人が実際に踏む問題なので 2026-09-21 にグローバル設定の口を戻した。
# ---------------------------------------------------------------------------

def is_enhanced_recall_available(model_key: Optional[str] = None) -> bool:
    """いま反射判断に選別を頼めるか (割り当てがあり、宛先が解決でき、キーも揃っているか)。

    ペルソナごとのスイッチ (AUTO_RECALL_ENHANCED) との **積** で ON/OFF が決まる。
    スイッチを入れただけでは判定は走らない — モデルの役割「反射判断」への割り当てと
    いう明示の行為が要る (黙って費用が発生する経路を作らないため。
    docs/intent/reflex_judgment.md §4)。

    Args:
        model_key: ペルソナ個別の上書き (``AI.REFLEX_JUDGMENT_MODEL``)。None なら
            世界の既定 (役割の env) を見る。

    ここの答えは判定の有無だけでなく **候補の集め方** にも効く (下の
    ``run_auto_recall`` が拾い上げを広げるかどうか)。だから判断層の側でも、キーが
    要る宛先のキー欠落まで含めて「使えない」と答える — 広げてから判定だけ落ちると、
    「従来方式へ戻った」というログと実際の挙動が食い違う。

    同じ理由で、**選別が投げる型 (noul) に答えられること**まで条件に入れる。choice に
    しか答えない宛先を「使える」と受け取ると、候補の集め方だけが広がったまま毎ターン
    判定が落ちる (= 上の食い違いが恒常化する)。

    使えない理由 (未割り当て / 設定が無い / キーと宛先の組が照合に通らない /
    noul に答えられない / キーが空) は判断層が WARNING に出す。判断層の
    読み込み自体に失敗しても会話は止めない (False を返すだけ)。
    """
    try:
        from saiverse.reflex_judgment import FIRST_STAGE_QUESTION_TYPE, is_available
    except Exception:
        LOGGER.warning(
            "[auto_recall][reflex] could not load the reflex judgment layer; "
            "staying on the embedding threshold", exc_info=True,
        )
        return False
    return is_available(model_key=model_key, required_type=FIRST_STAGE_QUESTION_TYPE)


# 反射判断に渡す候補の embed_score 下限。cosine 単独の採用しきい値 (0.86) より広く
# 取り、0.78〜0.86 帯の「拾えなかった正解」を判定に回す。
_REFLEX_FLOOR = 0.78

# 採用に必要な Noul 確率 (0〜1)。
_REFLEX_THRESHOLD = 0.5

# 反射判断を何秒まで待つか。自動想起は会話の同期経路にあるので有界に保つ。
#
# 2.5 は Jev の実験で決めた値だが、第 2 段で通常の LLM も答える側になり、クラウドの
# 軽量モデルは 1 往復 3.5〜4 秒かかることが実測で分かった (2026-09-21、Gemini 3.5
# Flash-Lite が毎ターン締切に届かず、判定が一度も成立しないのに課金だけ発生した)。
# 締切は「遅いときに見切る上限」なので、伸ばしても速い宛先 (Jev は 1 秒未満) の
# ターンは何も遅くならない — 遅くなるのは宛先が本当に固まっているターンの、
# 諦めるまでの待ちだけ。
#
# 上の 3 つと違ってこれだけ env の口を持つ (グローバル設定の画面から変えられる) のは、
# 使う人が実際に踏む問題だから — 割り当てたモデルが遅くて毎ターン従来方式へ戻る家では、
# ここを伸ばすのが直し方の一つになる。**モデルごとには置かない** (2026-09-21 まはー
# 裁定: これは「どのくらい待ったらフォールバックを発動させるか」のパラメータで、
# モデル依存の需要はほぼ無い)。値は毎回の呼び出し時に読むので、保存した次のターンから
# 効く (再起動は要らない)。
REFLEX_TIMEOUT_ENV = "SAIVERSE_REFLEX_TIMEOUT_SECONDS"
REFLEX_TIMEOUT_DEFAULT = 5.0
#: 画面から保存できる範囲。下は「速い宛先でも往復が入る最低限」、上は「会話を
#: 待たせてよい上限」。範囲外の値は画面側で丸める (ここは読むだけ)。
REFLEX_TIMEOUT_MIN = 1.0
REFLEX_TIMEOUT_MAX = 60.0


def get_reflex_timeout() -> float:
    """反射判断を何秒まで待つか (この秒数を過ぎたら見切って従来方式へ)。

    設定が無い / 数値として読めない / 0 以下や非有限の値のときは既定の 5 秒に倒す
    (会話の経路にいるので、設定の壊れで判定の待ちが 0 秒や無限になると、直し方の
    分からない形で会話が変わる)。壊れた値は WARNING に出す。

    範囲 (1〜60 秒) は**ここでも**効かせる — 画面からの保存は丸めてから書くが、
    .env を手で書けば範囲の外の値が入る。読む側が素通しすると、画面が約束している
    範囲 (GET /api/config/reflex-timeout の min/max) と実際の挙動がずれ、3600 の
    ような値が毎ターン会話を長時間止められる (2026-09-21 の敵対レビュー)。
    """
    value = _env_float(REFLEX_TIMEOUT_ENV, REFLEX_TIMEOUT_DEFAULT)
    if not math.isfinite(value) or value <= 0:
        LOGGER.warning(
            "[auto_recall] %s=%r is not a usable number of seconds; using the default %s",
            REFLEX_TIMEOUT_ENV, os.getenv(REFLEX_TIMEOUT_ENV), REFLEX_TIMEOUT_DEFAULT,
        )
        return REFLEX_TIMEOUT_DEFAULT
    if value < REFLEX_TIMEOUT_MIN or value > REFLEX_TIMEOUT_MAX:
        clamped = min(REFLEX_TIMEOUT_MAX, max(REFLEX_TIMEOUT_MIN, value))
        LOGGER.warning(
            "[auto_recall] %s=%r is outside the allowed range (%s-%s seconds); using %s",
            REFLEX_TIMEOUT_ENV, os.getenv(REFLEX_TIMEOUT_ENV),
            REFLEX_TIMEOUT_MIN, REFLEX_TIMEOUT_MAX, clamped,
        )
        return clamped
    return value

# 反射判断へ渡す state の会話本文メッセージ数 (直近から)。全件送りにしないのは、
# 「会話本文は 1 件 500 字まで」という有界性の根拠を件数側から崩さないため。
_REFLEX_CONTEXT_MESSAGES = 6


# ---------------------------------------------------------------------------
# 粘着ウィンドウ台帳 ((persona, thread) 単位のインメモリ状態)
# ---------------------------------------------------------------------------

# item_key = (source_type, source_id)。ledger のキーは (persona_id, thread_id)
# (2026-07-12 監査 P1: persona 単位キーだと thread 切替時に直前 thread で浮かんだ
# 記憶が新 thread の冒頭へ全件注入され、thread の文脈分離が破れていた)。
# CONVERSATION メインラインは現状 persona 単位 1 本なので line_id までは持たない。
#
# 仕様 (2026-07-12 裁定):
# - thread A → B → A と戻った場合、A の台帳が残っていれば自然に復元される
#   (キー分離による継続)。B 滞在中に A の台帳は老化しない (stale_turns は
#   その thread 自身のターンでのみ進む)。
# - 明示リセット (自動想起 OFF 時) は当該 persona の全 thread 分を破棄する。
# - 台帳数は persona あたり thread 数 (建物 + persona 既定 + 少数) で有限、
#   各台帳の中身も sticky_turns 超過で自然に空になるため、上限や掃除は設けない。


@dataclass
class _LedgerItem:
    """台帳エントリ 1 件。注入に必要な情報を丸ごと保持する。

    しきい値を割った後も ``stale_turns`` が sticky_turns を超えるまで注入し続ける
    ため、RecallHit 由来の表示材料をここに焼き込む (再検索で復元できない場合に備える)。
    """
    source_type: str
    source_id: str
    title: str
    content: str
    uri: str
    best_score: float
    # しきい値を「連続で」満たさなくなってからの経過ターン数。満たしたら 0 に戻す。
    stale_turns: int = 0
    # 鮮明想起 (§C, vivid recall) 用。source_type == "fragment" のときのみ意味を持つ
    # (RecallHit.chronicle_entry_id 由来)。rev4: これを持つ全 fragment 項目が対象
    # (先頭項目だけ最大2行、他は1行)。生成元 Chronicle エントリの source_ids_json
    # から「そのときの会話」を展開する。
    chronicle_entry_id: Optional[str] = None


@dataclass
class _LineLedger:
    items: Dict[Tuple[str, str], _LedgerItem] = field(default_factory=dict)


_LEDGERS: Dict[Tuple[str, str], _LineLedger] = {}
_LEDGERS_LOCK = threading.Lock()


def _get_ledger(persona_id: str, thread_id: str) -> _LineLedger:
    key = (persona_id, thread_id)
    with _LEDGERS_LOCK:
        led = _LEDGERS.get(key)
        if led is None:
            led = _LineLedger()
            _LEDGERS[key] = led
        return led


def reset_ledger(persona_id: str) -> None:
    """テスト用 / 明示リセット用。当該ペルソナの台帳を全 thread 分捨てる。"""
    with _LEDGERS_LOCK:
        for key in [k for k in _LEDGERS if k[0] == persona_id]:
            _LEDGERS.pop(key, None)


# ---------------------------------------------------------------------------
# クエリ生成
# ---------------------------------------------------------------------------

# クエリ / 除外判定の対象にする message role。head の system メッセージや
# realtime/visual メタメッセージは会話本文でないので除外する。
_QUERY_ROLES = frozenset({"user", "assistant"})


def _is_conversational_message(msg: Dict[str, Any]) -> bool:
    """クエリ生成・コンテキスト除外の対象になる「会話本文」メッセージか。"""
    role = msg.get("role")
    if role not in _QUERY_ROLES:
        return False
    metadata = msg.get("metadata") or {}
    # head 由来の user メッセージ (memory_weave / visual_context) や realtime は
    # 「今話している内容」ではないのでクエリから除外する。
    if metadata.get("__memory_weave_context__"):
        return False
    if metadata.get("__visual_context__"):
        return False
    if metadata.get("__realtime_context__"):
        return False
    if metadata.get("__auto_recall__"):
        return False
    # 送信直前に差し込まれる知覚ブロック (部屋の様子・通知) は role="user" で
    # 履歴へ時刻順マージされるが、ユーザーが言った言葉ではない。会話文として
    # 数えると、発話の後ろに挟まったブロックがクエリの種の座を奪う
    # (2026-09-06 に部屋の描画が head の __visual_context__ からこのブロックへ
    # 引っ越したとき、この除外リストが追従しなかったのが根因)。
    if metadata.get(CONSUMED_PERCEPTION_KEY):
        return False
    content = msg.get("content")
    return isinstance(content, str) and bool(content.strip())


def _latest_user_message(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """直近の user ターンのメッセージ dict を返す (本文の有無は問わない)。

    添付だけで本文が空のメッセージも拾えるよう、``_is_conversational_message``
    (content 非空必須) より緩い判定にする。head 由来の合成メッセージ
    (``__visual_context__`` 等) と知覚ブロックは除外する — 知覚ブロックは
    metadata に ``media`` を持ちうるので、除外しないと添付概要の取得元まで
    「部屋の様子」に乗っ取られる。
    """
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        metadata = m.get("metadata") or {}
        if (
            metadata.get("__memory_weave_context__")
            or metadata.get("__visual_context__")
            or metadata.get("__realtime_context__")
            or metadata.get("__auto_recall__")
            or metadata.get(CONSUMED_PERCEPTION_KEY)
        ):
            continue
        return m
    return None


def _current_attachment_summaries(messages: List[Dict[str, Any]]) -> List[str]:
    """最新 user メッセージに添付された画像/音声/動画の概要を集める。

    「今添付されたものだけ」をクエリに反映するため、最新の1件のメッセージだけを
    見る (過去メッセージの添付概要は拾わない)。概要は ``api/routes/chat.py`` が
    ``SAIVERSE_MEDIA_RECALL_ENABLED`` ON 時に同期生成して
    ``metadata["images"]`` / ``metadata["media"]`` の各エントリへ ``summary``
    キーで載せたもの (OFF 時はキー自体が存在しないので何も拾わない)。
    """
    msg = _latest_user_message(messages)
    if msg is None:
        return []
    metadata = msg.get("metadata") or {}
    summaries: List[str] = []
    for key in ("images", "media"):
        entries = metadata.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            summary = entry.get("summary")
            if isinstance(summary, str) and summary.strip():
                summaries.append(summary.strip())
    return summaries


# 知覚ブロックの content は ``<system>…</system>`` で包まれている。クエリや Jev の
# 判断材料に使うときは、この包みを外した本文だけを渡す。
_SYSTEM_WRAPPER_PATTERN = re.compile(r"\A\s*<system>\s*(.*?)\s*</system>\s*\Z", re.DOTALL)


def _strip_system_wrapper(text: str) -> str:
    """``<system>…</system>`` の包みを外す (包まれていなければそのまま返す)。"""
    match = _SYSTEM_WRAPPER_PATTERN.match(text)
    return match.group(1) if match else text


def _trailing_perception_blocks(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """末尾から遡り、最初の会話本文に当たるまでに現れた知覚ブロックを時刻順に返す。

    = 「このターンで新しく目に入ったもの」。会話本文でもブロックでもない行
    (head の system 行など) は素通りするだけで、遡りを止めない。
    """
    found: List[Dict[str, Any]] = []
    for m in reversed(messages):
        if is_injected_perception(m):
            found.append(m)
            continue
        if _is_conversational_message(m):
            break
    return list(reversed(found))


def _fresh_observation_texts(messages: List[Dict[str, Any]]) -> List[str]:
    """このターンで新しく目に入った知覚ブロックの本文を時刻順に返す。"""
    texts: List[str] = []
    for m in _trailing_perception_blocks(messages):
        body = _strip_system_wrapper(str(m.get("content") or "")).strip()
        if body:
            texts.append(body)
    return texts


def _seed_is_user_utterance(messages: List[Dict[str, Any]]) -> bool:
    """会話本文の末尾が user か = このターンに新しいユーザー発言があるか。"""
    for m in reversed(messages):
        if _is_conversational_message(m):
            return m.get("role") == "user"
    return False


def build_query(messages: List[Dict[str, Any]], *, n: Optional[int] = None) -> str:
    """直近 n 件の会話本文を連結してクエリ文字列にする。

    埋め込みの ``query:`` プレフィックス付与は unified_recall/embedder 側 (is_query=True)
    が行うので、ここでは生テキストの連結だけを返す。

    クエリの種の優先順位は 3 段 — **このターンのユーザー発言 > (発言が無ければ)
    直近の通知・部屋の様子 > (それも無ければ) 直近の assistant 発言**。知覚ブロック
    (部屋の様子・通知) は ``_is_conversational_message`` が会話文から外すので、
    ユーザーの発言より後ろに挟まっても種の座を奪わない。一方、このターンに新しい
    ユーザー発言が無い自律ターンでは、末尾の知覚ブロックが種になる (「見たものに
    対して想起が走る」経路は残す)。

    ``SAIVERSE_MEDIA_RECALL_ENABLED`` (グローバル設定) が ON のときは、最新
    user メッセージに添付された画像/音声/動画の概要 (chat.py が同期生成済み)
    をクエリ末尾に足す。OFF (既定) では何も足さず、従来どおりの挙動になる。
    """
    if n is None:
        n = get_query_message_count()
    convo = [m for m in messages if _is_conversational_message(m)]
    seed = convo
    if not _seed_is_user_utterance(messages):
        # このターンに新しいユーザー発言が無い (自律ターン / 冒頭)。末尾の知覚
        # ブロックがあればそれを種にする — 従来 (ブロックが会話文に数えられて
        # いた頃) と同じ「見たものに対して想起が走る」挙動。
        fresh = _fresh_observation_texts(messages)
        if fresh:
            seed = convo + [{"role": "user", "content": text} for text in fresh]
    tail = seed[-n:] if n > 0 else seed
    text_query = "\n".join(str(m.get("content", "")).strip() for m in tail).strip()

    if not is_media_recall_enabled():
        return text_query

    attachment_summaries = _current_attachment_summaries(messages)
    if not attachment_summaries:
        return text_query

    attachment_query = "\n".join(attachment_summaries)
    return f"{text_query}\n{attachment_query}".strip() if text_query else attachment_query


def _last_user_utterance(messages: List[Dict[str, Any]]) -> Optional[str]:
    """会話本文メッセージのうち role=user の最後の1件の content を返す。"""
    for m in reversed(messages):
        if not _is_conversational_message(m):
            continue
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return None


# ---------------------------------------------------------------------------
# エンティティトリガー (決定論、埋め込み検索と並行する第二の想起経路)
# ---------------------------------------------------------------------------

# タイトル照合の対象外にする最小タイトル長 (これ未満は誤爆しやすいので除外)。
_ENTITY_TITLE_MIN_LEN = 2


def _fetch_memopedia_titles(conn) -> List[Tuple[str, Optional[int], str, str]]:
    """is_deleted=0 の Memopedia ページ (id, short_id, title, summary) を全件取得する。

    毎ターン SELECT する想定 (約150件規模なのでコストは無視できる)。
    """
    try:
        cur = conn.execute(
            "SELECT id, short_id, title, summary FROM memopedia_pages "
            "WHERE (is_deleted = 0 OR is_deleted IS NULL) AND id NOT LIKE 'root_%'"
        )
        return [(row[0], row[1], row[2] or "", row[3] or "") for row in cur.fetchall()]
    except Exception:
        LOGGER.warning("[auto_recall] failed to fetch memopedia titles", exc_info=True)
        return []


def _find_entity_triggers(
    conn,
    messages: List[Dict[str, Any]],
) -> List[Tuple[str, Optional[int], str, str]]:
    """最新ユーザー発話にタイトルが部分文字列として含まれる Memopedia ページを検出する。

    ambient ガード: 「最新ユーザー発話を除く残りの会話本文メッセージ」でのタイトル
    出現回数が ``get_entity_ambient_count()`` 以上なら、そのタイトルはスキップする
    (エア・まはー・SAIVerse のような常在エンティティが毎ターン浮かぶのを防ぐ)。

    Returns:
        トリガーされたページのリスト (id, short_id, title, summary)。
    """
    last_utterance = _last_user_utterance(messages)
    if not last_utterance:
        return []

    titles = _fetch_memopedia_titles(conn)
    if not titles:
        return []

    # ambient カウント用: 最新ユーザー発話を除く残りの会話本文メッセージを連結。
    convo = [m for m in messages if _is_conversational_message(m)]
    # 最新ユーザー発話と同一の1件を除外 (末尾から最初に一致するものを除く)。
    remaining_texts: List[str] = []
    removed_last_user = False
    for m in reversed(convo):
        content = m.get("content")
        text = str(content).strip() if isinstance(content, str) else ""
        if not removed_last_user and m.get("role") == "user" and text == last_utterance.strip():
            removed_last_user = True
            continue
        if text:
            remaining_texts.append(text)
    ambient_blob = "\n".join(reversed(remaining_texts))

    ambient_limit = get_entity_ambient_count()
    triggered: List[Tuple[str, Optional[int], str, str]] = []
    for page_id, short_id, title, summary in titles:
        title = title.strip()
        if len(title) < _ENTITY_TITLE_MIN_LEN:
            continue
        if title not in last_utterance:
            continue
        ambient_count = ambient_blob.count(title) if ambient_blob else 0
        if ambient_count >= ambient_limit:
            LOGGER.debug(
                "[auto_recall] entity trigger skipped (ambient): title=%r count=%d >= %d",
                title, ambient_count, ambient_limit,
            )
            continue
        LOGGER.debug(
            "[auto_recall] entity trigger matched: title=%r page_id=%s short_id=%s",
            title, page_id, short_id,
        )
        triggered.append((page_id, short_id, title, summary))

    return triggered


def _context_message_ids(messages: List[Dict[str, Any]]) -> set:
    """現在のコンテキスト窓に載っている会話メッセージの id 集合。

    message ソースの想起ヒットのうち、既にコンテキストにある (= 二重注入になる)
    ものを除外するために使う (§4.1-3)。
    """
    ids = set()
    for m in messages:
        if not _is_conversational_message(m):
            continue
        mid = m.get("id")
        if mid:
            ids.add(str(mid))
    return ids


# ---------------------------------------------------------------------------
# 拾い上げの拡張 (「自動想起を強化する」が効いているときだけ通る経路)
#
# 実測で分かった狭さ: unified_recall の既定では message 枠が 1 席しかなく、その席を
# 「ユーザーが今言った発話そのもの」(直前に memory.db へ永続化済み、クエリと同一なので
# 類似度が最上位) が毎ターン占領し、後段で「既にコンテキストにある」として捨てられる。
# 過去の会話が浮かぶ経路が実質毎ターン死んでいた。加えてキーワード検索は
# ``query.split()`` 由来で、空白の無い日本語文では全文が 1 語になり何にもマッチしない。
# 設計と実測値は docs/intent/auto_recall_jev_rerank.md。
# ---------------------------------------------------------------------------

# クエリから内容語らしい連なりを抜く正規表現 (カタカナ連 / 漢字連 / 英数連)。
# 先頭の 1 文字だけ長音符「ー」と踊り字「々」を許さない — 「ねーエリス」の「ー」は
# 直前のひらがなに属するので、許すと語が「ーエリス」になって本文に一致しなくなる。
_KEYWORD_PATTERN = re.compile(r"[ァ-ヶ][ァ-ヶー]+|[一-龥][一-龥々]+|[A-Za-z0-9]{2,}")

# messages にこの件数を超えて出現する語は「ありふれた語」として捨てる。実験値
# (env にはしない — 実験の一部として調整する数字で、利用者向けの設定ではない)。
_KEYWORD_COMMON_LIMIT = 50

# 1 クエリから使うキーワードの上限 (出現件数の少ない順に選ぶ)。同上、実験値。
_KEYWORD_MAX_COUNT = 4

# 強化が効いているときのソース枠の上書き。message を 1 → 3 に広げる。合計 9 になるが
# topk=8 の天井はそのまま (枠は「上限」であって「確保」ではない)。
_ENHANCED_SOURCE_ALLOCATIONS = {"fragment": 5, "memopedia": 1, "message": 3}


def _extract_recall_keywords(
    conn, query: str, *, observations: Optional[List[str]] = None,
) -> List[str]:
    """クエリから「珍しい内容語」を最大 ``_KEYWORD_MAX_COUNT`` 個抜き出す。

    ありふれた語を落とすのは、エンティティトリガーの ambient ガードと同じ思想 —
    「珍しい名前ほど連想が走る」。unified_recall ではキーワード一致数が第一ソート
    キーなので、常連の語 (ペルソナ名など) を残すと毎ターン同じ大量のヒットが
    上位を占め、枠を食い潰してしまう。

    ``observations`` は「このターンで新しく目に入ったもの」(部屋の様子・通知) の
    本文。クエリの種はユーザーの発言のままにしつつ、見えたものの中の珍しい語も
    字面検索の脇道から参加させる。珍しさの判定も上限 ``_KEYWORD_MAX_COUNT`` の枠も
    クエリ本文と共通 — 見えたものが枠を独り占めしないよう、クエリ本文の語を先に
    並べて同数のときの安定ソートで前に来るようにする。

    失敗しても会話は止めない。空リストを返すと呼び出し側は ``keywords`` を渡さず、
    unified_recall の従来どおりの ``query.split()`` 経路に戻る。
    """
    sources = [query or ""] + [text for text in (observations or []) if text]
    if not any(s.strip() for s in sources):
        return []

    try:
        from sai_memory.unified_recall import _escape_like

        words: List[str] = []
        seen: set = set()
        for source in sources:
            for match in _KEYWORD_PATTERN.finditer(source):
                word = match.group(0)
                if word in seen:
                    continue
                seen.add(word)
                words.append(word)
        if not words:
            return []

        kept: List[Tuple[str, int]] = []
        dropped: List[Tuple[str, int]] = []
        for word in words:
            cur = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE content LIKE ? ESCAPE '\\'",
                (f"%{_escape_like(word)}%",),
            )
            row = cur.fetchone()
            count = int(row[0]) if row else 0
            if 1 <= count <= _KEYWORD_COMMON_LIMIT:
                kept.append((word, count))
            else:
                dropped.append((word, count))

        # 出現件数の少ない順 (= 珍しい順)。同数は出現順のまま (sort は安定)。
        kept.sort(key=lambda wc: wc[1])
        selected = kept[:_KEYWORD_MAX_COUNT]
        LOGGER.debug(
            "[auto_recall][reflex] keywords: %s (dropped: %s)",
            " ".join(f"{w}={c}" for w, c in selected) or "(none)",
            " ".join(f"{w}={c}" for w, c in dropped) or "(none)",
        )
        return [w for w, _ in selected]
    except Exception:
        LOGGER.warning(
            "[auto_recall][reflex] keyword extraction failed; "
            "falling back to the default whitespace split",
            exc_info=True,
        )
        return []


# ---------------------------------------------------------------------------
# 選別 + 台帳更新 + フォーマット
# ---------------------------------------------------------------------------

# 由来ラベルの depth ハンドル (深掘りスペル名)。§4.2。
# rev3 (2026-07-04): 各行末尾の個別表記は廃止し、ブロック末尾に含まれたソース種別の
# union を 1 行だけ出す (フッター行、_format_footer 参照)。
_HANDLE_BY_SOURCE = {
    "chronicle": "chronicle_read_detail",
    "memopedia": "memory_read",
    "fragment": "memory_read",
    "message": "memory_recall_unified",
}


def _format_source_label(item: _LedgerItem) -> str:
    """由来ラベル ([Chronicle 日付] / [Memopedia: タイトル m:N] / [過去の会話 日付]) を作る。"""
    st = item.source_type
    if st == "chronicle":
        # title は "Chronicle Lv1: 2025-06-07 12:00 ~ ..." の形。日付部を抜く。
        return f"[Chronicle] {item.title}"
    if st in ("memopedia", "fragment"):
        if not item.title:
            # 孤児 Fragment (親ページが物理削除済み)。本文だけの記憶の断片として提示。
            return "[記憶の断片]"
        return f"[Memopedia: {item.title}]"
    if st == "message":
        return f"[過去の会話 {item.title}]"
    return f"[{st}] {item.title}"


def _item_handle(item: _LedgerItem) -> Optional[str]:
    """このアイテムを深掘りするためのスペル名 (フッターの union 計算用)。"""
    if item.source_type in ("memopedia", "fragment") and not item.title:
        # 孤児 Fragment はページが無いので memory_read では深掘りできない。
        return "memory_recall_unified"
    return _HANDLE_BY_SOURCE.get(item.source_type)


# ---------------------------------------------------------------------------
# 鮮明想起 (§C, vivid recall) — rev4: 注入ブロック内の全 fragment 項目に展開する。
# 先頭項目だけ最大2行 (鮮明度の勾配)、それ以外は1行。文字数予算は全項目で共有。
# ---------------------------------------------------------------------------

# vivid 展開で添える生ログの最大件数 (先頭アイテム)。
_VIVID_MAX_MESSAGES_TOP = 2
# vivid 展開で添える生ログの最大件数 (先頭以外のアイテム)。
_VIVID_MAX_MESSAGES_REST = 1
# メッセージ本文のプレビュー長 (文字) — チャンク抜粋の再トリムに使う旧上限。
# rev4 ではタイトル出現位置を中心にした ±60字窓 (_VIVID_TITLE_WINDOW) を優先し、
# タイトルがチャンク内に無い場合のみこちらでトリムする。
_VIVID_CONTENT_PREVIEW = 120
# チャンク内にページタイトルが見つかった場合の前後窓幅 (まはーの案: 名前の周辺だけ)。
_VIVID_TITLE_WINDOW = 60


def _cosine(a: List[float], b: List[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / ((norm_a ** 0.5) * (norm_b ** 0.5))


def _title_windowed_excerpt(text: str, title: str) -> Optional[str]:
    """text 内に title の出現があれば、その位置を中心に ±_VIVID_TITLE_WINDOW 字へ詰める。

    見つからなければ None (呼び出し側は元テキストをそのまま使うか、フォールバック
    経路を試みる)。
    """
    if not text or not title:
        return None
    pos = text.find(title)
    if pos == -1:
        return None
    start = max(0, pos - _VIVID_TITLE_WINDOW)
    end = min(len(text), pos + len(title) + _VIVID_TITLE_WINDOW)
    excerpt = text[start:end].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{excerpt}{suffix}"


def _source_message_ids(conn, chronicle_entry_id: str) -> List[str]:
    """chronicle_entry_id の source_ids_json から message_id 列を取り出す。"""
    import json
    try:
        cur = conn.execute(
            "SELECT source_ids_json FROM arasuji_entries WHERE id = ?",
            (chronicle_entry_id,),
        )
        row = cur.fetchone()
    except Exception:
        LOGGER.debug("[auto_recall][vivid] failed to fetch arasuji_entries.source_ids_json", exc_info=True)
        return []
    if not row or not row[0]:
        return []
    try:
        message_ids = json.loads(row[0])
    except (ValueError, TypeError):
        LOGGER.debug("[auto_recall][vivid] source_ids_json parse failed: %r", row[0])
        return []
    if not isinstance(message_ids, list) or not message_ids:
        return []
    return [str(m) for m in message_ids]


def _vivid_source_messages(
    conn, item: _LedgerItem, *, max_messages: int,
) -> List[Tuple[str, str, int, Optional[int], float]]:
    """Fragment 埋め込み由来のベストマッチメッセージ・チャンクを抜粋して返す。

    生成元 Chronicle エントリの source_ids_json から、Fragment 埋め込みに最も近い
    メッセージを最大 max_messages 件選び、そのベストチャンクをタスク1の
    reconstruct_message_excerpt() で復元する (書き込み時と同じ min/max_chars で
    再分割 + 整合性ガード)。さらにチャンク内にページタイトルがあれば ±60字へ詰める。

    埋め込みが1件も無ければ空リストを返す (呼び出し側はフォールバック経路を試みる。
    LLM は絶対に呼ばない)。

    Returns:
        (role, excerpt, created_at, chunk_index, cosine_score) のリスト。
        chunk_index は整合性ガードでフォールバックした場合 None。
    """
    if item.source_type != "fragment" or not item.chronicle_entry_id:
        return []

    message_ids = _source_message_ids(conn, item.chronicle_entry_id)
    if not message_ids:
        return []

    import json
    try:
        cur = conn.execute(
            "SELECT vector FROM memopedia_fragment_embeddings WHERE fragment_id = ?",
            (item.source_id,),
        )
        frag_row = cur.fetchone()
    except Exception:
        LOGGER.debug("[auto_recall][vivid] failed to fetch fragment embedding", exc_info=True)
        return []
    if not frag_row or not frag_row[0]:
        LOGGER.debug("[auto_recall][vivid] no fragment embedding for %s; try fallback", item.source_id)
        return []

    try:
        frag_vec = json.loads(frag_row[0])
    except (ValueError, TypeError):
        return []

    placeholders = ",".join("?" for _ in message_ids)
    try:
        cur = conn.execute(
            f"SELECT message_id, chunk_index, vector FROM message_embeddings "
            f"WHERE message_id IN ({placeholders})",
            message_ids,
        )
        chunk_rows = cur.fetchall()
    except Exception:
        LOGGER.debug("[auto_recall][vivid] failed to fetch message_embeddings", exc_info=True)
        return []

    if not chunk_rows:
        LOGGER.debug(
            "[auto_recall][vivid] no message_embeddings for chronicle_entry_id=%s; try fallback",
            item.chronicle_entry_id,
        )
        return []

    # message_id ごとに best (score, chunk_index) を取る (チャンク分割されているため)。
    best_by_message: Dict[str, Tuple[float, int]] = {}
    for mid, chunk_idx, vec_json in chunk_rows:
        try:
            vec = json.loads(vec_json)
        except (ValueError, TypeError):
            continue
        score = _cosine(frag_vec, vec)
        prev = best_by_message.get(mid)
        if prev is None or score > prev[0]:
            best_by_message[mid] = (score, int(chunk_idx) if chunk_idx is not None else 0)

    if not best_by_message:
        LOGGER.debug("[auto_recall][vivid] no comparable embeddings for fragment=%s; try fallback", item.source_id)
        return []

    top_ids = sorted(best_by_message.keys(), key=lambda mid: -best_by_message[mid][0])[:max_messages]

    placeholders2 = ",".join("?" for _ in top_ids)
    try:
        cur = conn.execute(
            f"SELECT id, role, content, created_at FROM messages WHERE id IN ({placeholders2})",
            top_ids,
        )
        msg_rows = {r[0]: r for r in cur.fetchall()}
    except Exception:
        LOGGER.debug("[auto_recall][vivid] failed to fetch messages", exc_info=True)
        return []

    from sai_memory.unified_recall import _EXCERPT_TRIM, reconstruct_message_chunk

    result: List[Tuple[str, str, int, Optional[int], float]] = []
    for mid in top_ids:
        row = msg_rows.get(mid)
        if not row:
            continue
        _id, role, full_content, created_at = row
        score, chunk_idx = best_by_message[mid]
        # トリムなしのチャンク全文を復元してから、タイトル出現位置での ±60字窓を
        # 試みる (先に 200字トリムすると、タイトルがトリム範囲外に落ちてしまうため)。
        chunk, used_chunk_index = reconstruct_message_chunk(conn, mid, chunk_idx, full_content or "")
        titled = _title_windowed_excerpt(chunk, item.title) if item.title else None
        if titled is not None:
            final_excerpt = titled
        else:
            final_excerpt = chunk[:_EXCERPT_TRIM] + ("…" if len(chunk) > _EXCERPT_TRIM else "")
        result.append((role or "unknown", final_excerpt, created_at or 0, used_chunk_index, score))
        LOGGER.debug(
            "[auto_recall][vivid] fragment=%s selected message id=%s role=%s chunk_index=%s score=%.3f titled=%s",
            item.source_id, mid, role, used_chunk_index, score, titled is not None,
        )

    LOGGER.debug(
        "[auto_recall][vivid] fragment=%s chronicle_entry_id=%s candidates=%d selected=%d",
        item.source_id, item.chronicle_entry_id, len(best_by_message), len(result),
    )
    return result


def _vivid_fallback_messages(
    conn, item: _LedgerItem, *, max_messages: int,
) -> List[Tuple[str, str, int, Optional[int], float]]:
    """埋め込み経路が空のときのフォールバック: ページタイトルの素朴な文字列検索。

    chronicle_entry_id 経由の元メッセージ群を新しい順に見て、本文にページタイトルが
    含まれるものを最大 max_messages 件、±60字窓で抜粋する。タイトルが1件も見つから
    なければ空リスト (呼び出し側はその項目の vivid をスキップする)。LLM は呼ばない。
    """
    if item.source_type != "fragment" or not item.chronicle_entry_id or not item.title:
        return []

    message_ids = _source_message_ids(conn, item.chronicle_entry_id)
    if not message_ids:
        return []

    placeholders = ",".join("?" for _ in message_ids)
    try:
        cur = conn.execute(
            f"SELECT id, role, content, created_at FROM messages WHERE id IN ({placeholders}) "
            f"ORDER BY created_at DESC",
            message_ids,
        )
        rows = cur.fetchall()
    except Exception:
        LOGGER.debug("[auto_recall][vivid] fallback failed to fetch messages", exc_info=True)
        return []

    result: List[Tuple[str, str, int, Optional[int], float]] = []
    for _id, role, content, created_at in rows:
        if len(result) >= max_messages:
            break
        if not content or item.title not in content:
            continue
        excerpt = _title_windowed_excerpt(content, item.title)
        if excerpt is None:
            continue
        result.append((role or "unknown", excerpt, created_at or 0, None, -1.0))
        LOGGER.debug(
            "[auto_recall][vivid] fragment=%s fallback matched message id=%s role=%s (plain title search)",
            item.source_id, _id, role,
        )

    LOGGER.debug(
        "[auto_recall][vivid] fragment=%s fallback candidates=%d selected=%d",
        item.source_id, len(rows), len(result),
    )
    return result


def _vivid_messages_for_item(
    conn, item: _LedgerItem, *, max_messages: int,
) -> List[Tuple[str, str, int, Optional[int], float]]:
    """埋め込み経路を試し、空ならタイトル文字列検索へフォールバックする。"""
    try:
        messages = _vivid_source_messages(conn, item, max_messages=max_messages)
    except Exception:
        LOGGER.warning(
            "[auto_recall][vivid] embedding-path expansion raised for %s/%s",
            item.source_type, item.source_id, exc_info=True,
        )
        messages = []
    if messages:
        return messages

    try:
        fallback = _vivid_fallback_messages(conn, item, max_messages=max_messages)
    except Exception:
        LOGGER.warning(
            "[auto_recall][vivid] fallback expansion raised for %s/%s",
            item.source_type, item.source_id, exc_info=True,
        )
        fallback = []
    if not fallback:
        LOGGER.debug(
            "[auto_recall][vivid] skip vivid for %s/%s: no embedding candidates and no title match",
            item.source_type, item.source_id,
        )
    return fallback


def _format_vivid_lines(
    messages: List[Tuple[str, str, int, Optional[int], float]], *, char_budget: int,
) -> List[str]:
    """「そのときの会話」サブ行を組み立てる。合計文字数が char_budget を超えたら以降を捨てる。"""
    from datetime import datetime

    lines: List[str] = []
    total = 0
    for role, content, created_at, _chunk_index, _score in messages:
        date_str = datetime.fromtimestamp(created_at).strftime("%Y-%m-%d") if created_at else "?"
        preview = content.strip().replace("\n", " ")
        if len(preview) > _VIVID_CONTENT_PREVIEW:
            preview = preview[:_VIVID_CONTENT_PREVIEW] + "…"
        line = f"　└ そのときの会話 ({date_str}): {role}「{preview}」"
        if total + len(line) + 1 > char_budget and lines:
            break
        lines.append(line)
        total += len(line) + 1
    return lines


def _format_line(item: _LedgerItem, *, max_content: int = 160) -> str:
    label = _format_source_label(item)
    body = (item.content or "").strip().replace("\n", " ")
    if len(body) > max_content:
        body = body[:max_content] + "…"
    return f"- {label} {body}"


def _format_footer(items: List[_LedgerItem]) -> str:
    """含まれたアイテムのハンドル union を1行で返す (§B: 冗長排除)。"""
    handles: List[str] = []
    for item in items:
        h = _item_handle(item)
        if h and h not in handles:
            handles.append(h)
    return f"（深掘り用スペル: {' / '.join(handles)}）"


# ---------------------------------------------------------------------------
# 反射判断による選別の実行 (効いているときだけ通る経路)
# ---------------------------------------------------------------------------

_REFLEX_CRITERION_TRUE = (
    "記憶の内容が現在の話題・登場人物・状況と具体的に結びついており、"
    "いま思い出すことが会話の理解や応答に寄与する。"
)
_REFLEX_CRITERION_FALSE = (
    "話題が違う、または表面的な語の類似だけで、"
    "いまの会話に持ち込むと不自然・無関係になる。"
)

# 判断へ送る会話本文 1 件あたりの文字数上限。
#
# 上限が無いと 1 ターンのペイロードが発話の長さに引きずられて青天井になる: 費用の
# 見積もり (1 ターン 0.01 円未満) が崩れ、長話のターンほど送受信が伸びて絶対締切に
# 掛かりやすくなる — つまり「長く話したターンほど判定が効かない」という、狙いと
# 逆の挙動になる。候補記憶の側は unified_recall が既に 200 字抜粋にしているので、
# ここで切るのは会話本文だけ。
_REFLEX_CONVERSATION_TEXT_LIMIT = 500

# state に入れる「いま見えたもの」(部屋の様子・通知) の最大件数 (最新側から)。
# 会話本文と同じく、1 ターンのペイロードが目に入ったものの量で青天井にならない
# ようにするための有界化。
_REFLEX_MAX_OBSERVATIONS = 3


def _clip(text: str, limit: int) -> str:
    """``limit`` 字を超えるテキストを先頭 ``limit`` 字 + 省略記号に詰める。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _reflex_candidates(
    hits,
    *,
    accepted_keys: set,
    context_ids: set,
    floor: float,
) -> List[Tuple[Tuple[str, str], Any]]:
    """反射判断に判定させる候補 (key, hit) を選ぶ。

    既存の選別ループが無条件に捨てるもの (エンティティトリガーで採用済み / 既に
    コンテキスト窓にある message / embed_score なし) はここでも外す。残りのうち
    ``embed_score >= floor`` のものが候補。
    """
    candidates: List[Tuple[Tuple[str, str], Any]] = []
    for hit in hits:
        key = (hit.source_type, str(hit.source_id))
        if key in accepted_keys:
            continue
        if hit.source_type == "message" and str(hit.source_id) in context_ids:
            continue
        if hit.embed_score is None:
            continue
        if hit.embed_score < floor:
            continue
        candidates.append((key, hit))
    return candidates


def _build_reflex_request(
    messages: List[Dict[str, Any]],
    candidates: List[Tuple[Tuple[str, str], Any]],
    *,
    context_messages: int,
    observations: Optional[List[str]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, Tuple[str, str]]]:
    """反射判断に渡す state / questions と、qid → 台帳キーの対応表を組み立てる。

    会話本文は 1 件あたり ``_REFLEX_CONVERSATION_TEXT_LIMIT`` 字で切る (理由は同定数の
    コメント)。候補記憶の本文は unified_recall が既に 200 字抜粋にしているので
    ここでは触らない。

    メディア想起 (``SAIVERSE_MEDIA_RECALL_ENABLED``) が ON のときは、検索クエリ
    (``build_query``) に足すのと同じ「最新添付の概要」を state にも入れる。検索が
    写真をきっかけに拾ってきた候補を、写真を知らない判断が落としてしまう非対称を
    避けるため。OFF のときと概要が無いときは ``attachments`` キー自体を入れない
    (state の形は従来のまま)。

    ``observations`` は「このターンで新しく目に入ったもの」(部屋の様子・通知) の
    本文。ユーザーの発言が種になったターンでは知覚ブロックが ``conversation`` に
    入らないので、ここから別枠で渡す — 見えたものを知らない判断が、見えたものに
    紐づく候補を落とす非対称を避けるため。会話本文と同じく 1 件
    ``_REFLEX_CONVERSATION_TEXT_LIMIT`` 字で切り、最新側から最大
    ``_REFLEX_MAX_OBSERVATIONS`` 件。無いときは ``observations`` キー自体を入れない。
    """
    convo = [m for m in messages if _is_conversational_message(m)]
    # 呼び出し側が >= 1 を保証する (_REFLEX_CONTEXT_MESSAGES)。クエリ側の
    # 「0 以下 = 全件」の慣習をここで実装すると、将来別の呼び出し元が付いたときに
    # 会話履歴の全件が外部 API へ出る経路が黙って復活するので、持ち込まない。
    tail = convo[-max(1, context_messages):]
    conversation = [
        {
            "role": str(m.get("role") or ""),
            "text": _clip(str(m.get("content", "")).strip(), _REFLEX_CONVERSATION_TEXT_LIMIT),
        }
        for m in tail
    ]

    memories: Dict[str, Dict[str, str]] = {}
    questions: Dict[str, Dict[str, Any]] = {}
    key_by_qid: Dict[str, Tuple[str, str]] = {}
    for idx, (key, hit) in enumerate(candidates):
        qid = f"m{idx}"
        key_by_qid[qid] = key
        memories[qid] = {"title": hit.title or "", "content": hit.content or ""}
        questions[qid] = {
            "type": "noul",
            "instructions": (
                f"`memories.{qid}` の記憶は、`conversation` の現在の話の流れの中で、"
                "会話の当事者の頭に自然に浮かぶ関連記憶か。"
            ),
            "criteria": {"true": _REFLEX_CRITERION_TRUE, "false": _REFLEX_CRITERION_FALSE},
        }

    state: Dict[str, Any] = {"conversation": conversation, "memories": memories}
    if is_media_recall_enabled():
        attachment_summaries = _current_attachment_summaries(messages)
        if attachment_summaries:
            state["attachments"] = attachment_summaries
    if observations:
        state["observations"] = [
            _clip(text, _REFLEX_CONVERSATION_TEXT_LIMIT)
            for text in observations[-_REFLEX_MAX_OBSERVATIONS:]
        ]
    return state, questions, key_by_qid


def _run_reflex_rerank(
    hits,
    messages: List[Dict[str, Any]],
    *,
    accepted_keys: set,
    context_ids: set,
    persona_id: str,
    observations: Optional[List[str]] = None,
    reflex_model_key: Optional[str] = None,
) -> Tuple[Optional[Dict[Tuple[str, str], float]], bool]:
    """反射判断に候補を一括判定させる。

    Returns:
        ``(判定結果, 締切超過だったか)`` の組。

        - 判定結果が ``{key: noul}``: 判定できた (候補ゼロなら空 dict。判断は
          呼んでいない)。
        - 判定結果が ``None``: 反射判断が使えなかった。呼び出し側はこのターンだけ
          既存の cosine しきい値方式へ静かに戻る (外部 API の障害でペルソナの返事を
          止めないため)。

        第 2 要素は「時間内に答えなかったせいで戻った」ターンだけ True。他の失敗
        (キー欠落・接続失敗・不正応答・準備の失敗) では False — 待ち時間を延ばしても
        速い宛先に替えても直らない失敗を、直し方の書いてある注記で案内しないため。
    """
    # 準備段階 (答える側の解決・候補の絞り込み・リクエスト組み立て・遅延 import) は
    # まるごと try の中に置く。docstring が約束する「失敗は None の 1 種類」を関数
    # 全体で成立させるため (隣人の unified_recall / _find_entity_triggers も同じ姿勢で
    # 丸ごと包んでいる)。遅延 import は OFF 経路で reflex_judgment (httpx) を一切
    # 読み込ませないためのもので、その失敗もここで畳む。
    try:
        from saiverse.reflex_judgment import (
            DEADLINE_MARGIN,
            FIRST_STAGE_QUESTION_TYPE,
            UNAVAILABLE_DEADLINE,
            ReflexJudgmentUnavailable,
            evaluate,
            resolve_backend,
        )

        accept_threshold = get_similarity_threshold()
        candidates = _reflex_candidates(
            hits, accepted_keys=accepted_keys, context_ids=context_ids, floor=_REFLEX_FLOOR,
        )

        # 設定ミスの警告は「実際に記憶が落ちうるターン」でだけ出す。ヒットが 1 件も
        # 無いターンにも出すと、何も起きていないのに毎ターン警告が並び、警告の意味
        # (この設定で記憶が落ちている) と実際に起きたことがずれる。
        if hits and _REFLEX_FLOOR > accept_threshold:
            LOGGER.warning(
                "[auto_recall][reflex] the candidate floor %.3f is above the cosine "
                "acceptance threshold %.3f: memories that the conventional path would "
                "have injected (embed_score between %.3f and %.3f) never reach the "
                "judgment and are silently dropped (persona=%s)",
                _REFLEX_FLOOR, accept_threshold, accept_threshold, _REFLEX_FLOOR, persona_id,
            )

        if not candidates:
            LOGGER.debug(
                "[auto_recall][reflex] no candidate above floor; judgment not called (persona=%s)",
                persona_id,
            )
            return {}, False

        # 答える側は呼ぶ直前に解決する (判定ログにどのモデル設定が答えたかを載せる —
        # docs/intent/reflex_judgment.md §5)。宛先 (ペルソナ個別の上書きを含む) と
        # 要求する型は「使えるか」の判定 (is_enhanced_recall_available) と揃える —
        # 判定と実呼び出しの間に設定が
        # 読み直された場合でも、この解決が照合ごと全部やり直すので、検査を通らない
        # 宛先へ飛ぶことはない (型が合わなければここで不成立 → 従来方式へ)。
        backend = resolve_backend(
            model_key=reflex_model_key, required_type=FIRST_STAGE_QUESTION_TYPE,
        )
        state, questions, key_by_qid = _build_reflex_request(
            messages, candidates,
            context_messages=_REFLEX_CONTEXT_MESSAGES,
            observations=observations,
        )
    except Exception:
        LOGGER.warning(
            "[auto_recall][reflex] could not prepare the request (import, backend or "
            "build failed); falling back to cosine threshold for this turn (persona=%s)",
            persona_id, exc_info=True,
        )
        return None, False

    started = time.monotonic()
    # 利用者の設定値 (get_reflex_timeout) は「この秒数を過ぎたら見切る」の約束。
    # 判断層の見切りは timeout + 内部の余裕 (DEADLINE_MARGIN) で行われるので、
    # 設定値をそのまま渡すと実際の見切りが約束より 0.5 秒遅くなる — 余裕ぶんを
    # 引いて渡し、見切りの時刻を設定値そのものに合わせる (2026-09-21 の敵対レビュー)。
    wait_seconds = max(0.5, get_reflex_timeout() - DEADLINE_MARGIN)
    try:
        nouls, usage = evaluate(
            state, questions,
            timeout=wait_seconds, backend=backend, persona_id=persona_id,
        )
    except ReflexJudgmentUnavailable as exc:
        deadline = getattr(exc, "kind", None) == UNAVAILABLE_DEADLINE
        LOGGER.warning(
            "[auto_recall][reflex] unavailable (%s); falling back to cosine threshold "
            "for this turn (persona=%s, model=%s)", exc, persona_id, backend.model_key,
        )
        return None, deadline
    except Exception:
        LOGGER.warning(
            "[auto_recall][reflex] rerank raised; falling back to cosine threshold "
            "for this turn (persona=%s, model=%s)", persona_id, backend.model_key, exc_info=True,
        )
        return None, False

    latency_ms = (time.monotonic() - started) * 1000.0
    decisions = {key_by_qid[qid]: noul for qid, noul in nouls.items() if qid in key_by_qid}
    LOGGER.info(
        "[auto_recall][reflex] judged %d/%d candidate(s) in %.0f ms "
        "(persona=%s, model=%s, usage=%s)",
        len(decisions), len(candidates), latency_ms, persona_id, backend.model_key, usage,
    )
    return decisions, False


@dataclass
class AutoRecallResult:
    """auto_recall の実行結果。"""
    injected: bool
    block: Optional[str]        # 注入する <system> ブロック本文 (injected=True のとき)
    query: str
    hit_count: int              # unified_recall が返した総ヒット数
    accepted_count: int         # しきい値通過 (今ターン新規採用) 件数
    ledger_size: int            # 台帳に残っている総件数
    char_count: int             # ブロックの文字数
    # <system>/</system> タグを剥がした本文 (injected=True のとき)。ChatMessage.auto_recall
    # への永続化用 (sea/runtime_context.py の persona._pending_auto_recall_text 経由)。
    # LLM コンテキストには一切使わない — block (タグ付き) だけが履歴末尾に注入される。
    plain_text: Optional[str] = None
    # このターンは反射判断が**時間内に答えなかった**ので従来方式へ戻った。呼び出し側
    # (sea/runtime_context.py) が画面の注記として知らせるためだけの旗で、注入の中身には
    # 一切影響しない。他の失敗 (キー欠落・接続失敗・不正応答) では立たない — 待ち時間や
    # モデルの割り当てを変えれば直る失敗だけを、直し方つきで知らせるため。
    reflex_deadline_fallback: bool = False


def run_auto_recall(
    conn,
    embedder,
    messages: List[Dict[str, Any]],
    *,
    persona_id: str,
    thread_id: str,
    enhanced: bool = False,
    reflex_model_key: Optional[str] = None,
) -> AutoRecallResult:
    """自動想起を 1 ターン分実行し、注入ブロック (あれば) を返す。

    LLM は呼ばない (唯一の外部呼び出しは、強化が効いているときの反射判断 1 往復。
    文章生成はしない)。呼び出し側はスコープ (CONVERSATION アスペクト) を確認済みで
    あること。``conn`` / ``embedder`` が None の場合は no-op (注入なし)。

    Args:
        conn: persona の memory.db 接続 (adapter.conn)。
        embedder: Embedder インスタンス (adapter.embedder)。
        messages: prepare_context が組み立てた message 列 (head + 履歴)。クエリ生成と
            「既にコンテキストにある内容」の除外に使う。注入位置の決定は呼び出し側。
        persona_id: 台帳キーの第1要素。
        thread_id: 台帳キーの第2要素。呼び出し側が adapter から取得した canonical
            thread_id (メインライン履歴の読み書きと同じ解決) を明示的に渡す。
            thread を跨いで粘着記憶が持ち込まれない境界 (2026-07-12 監査 P1)。
        enhanced: ペルソナごとの「自動想起を強化する」スイッチ (DB の
            ``AI.AUTO_RECALL_ENHANCED``)。この関数は persona_id 文字列しか持たないので、
            persona オブジェクトを持つ呼び出し側 (sea/runtime_context.py) が読んで渡す。
            既定 False = 従来どおりの埋め込みしきい値判定 (挙動は 1 ビットも変わらない)。
        reflex_model_key: ペルソナ個別の反射判断モデル (DB の
            ``AI.REFLEX_JUDGMENT_MODEL``)。``enhanced`` と同じく、persona オブジェクトを
            持つ呼び出し側が読んで渡す。None なら世界の既定 (モデルの役割「反射判断」の
            env) に落ちる。

    Returns:
        AutoRecallResult。``injected`` が True のとき ``block`` を末尾注入する。
        ``reflex_deadline_fallback`` が True のターンは、反射判断が時間内に答えず
        従来方式へ戻った (呼び出し側が画面の注記として知らせる)。
    """
    if conn is None or embedder is None:
        LOGGER.debug("[auto_recall] conn/embedder unavailable; skip (persona=%s)", persona_id)
        return AutoRecallResult(False, None, "", 0, 0, 0, 0)

    query = build_query(messages)

    threshold = get_similarity_threshold()
    msg_threshold_offset = get_message_threshold_offset()
    sticky_turns = get_sticky_turns()
    topk = get_topk()

    context_ids = _context_message_ids(messages)
    ledger = _get_ledger(persona_id, thread_id)

    # 強化が効くのは「ペルソナのスイッチ ON」かつ「反射判断が使える」ときだけ。
    # スイッチだけでは走らない (役割にモデルを割り当てるという明示の行為が要る)。
    # 効いているかどうかは「どこまで拾い上げるか」にも効く (効くときだけ広げる)。
    # 効いていないときは unified_recall へ渡す引数が従来と 1 ビットも変わらないよう、
    # 追加引数そのものを渡さない。
    reflex_enabled = bool(enhanced) and is_enhanced_recall_available(reflex_model_key)

    # 「このターンで新しく目に入ったもの」(部屋の様子・通知)。ユーザーの発言が種に
    # なったターンでだけ集める — 発言が無いターンでは build_query がこれ自体を種に
    # しているので、脇道から重ねて渡す意味がない。使うのは強化の経路だけ
    # (字面検索のキーワードと、反射判断の判断材料)。
    observations: List[str] = (
        _fresh_observation_texts(messages)
        if reflex_enabled and _seed_is_user_utterance(messages)
        else []
    )

    # --- エンティティトリガー経路 (決定論・埋め込み検索と並行) ---
    accepted_keys: set = set()
    accepted_count = 0
    try:
        entity_hits = _find_entity_triggers(conn, messages)
    except Exception:
        LOGGER.warning("[auto_recall] entity trigger raised (persona=%s)", persona_id, exc_info=True)
        entity_hits = []

    for page_id, short_id, title, summary in entity_hits:
        key = ("memopedia", str(page_id))
        accepted_keys.add(key)
        accepted_count += 1
        display_title = f"{title} (memopedia:{short_id})" if short_id is not None else title
        existing = ledger.items.get(key)
        if existing is not None:
            existing.stale_turns = 0
            existing.best_score = 1.0
            existing.title = display_title
            existing.content = summary or existing.content
        else:
            ledger.items[key] = _LedgerItem(
                source_type="memopedia",
                source_id=str(page_id),
                title=display_title,
                content=summary or "",
                uri=to_uri("memopedia", short_id if short_id is not None else page_id),
                best_score=1.0,
                stale_turns=0,
            )

    def _collect(extra_kwargs: Dict[str, Any]):
        """候補を集める。失敗しても会話は止めない (None を返し、呼び出し側が畳む)。

        ``extra_kwargs`` が空のときが従来の集め方で、強化が効いているターンだけ
        キーワード・除外・枠の上書きが乗る。戻り値の None は「集め損ねた」の印 —
        「候補ゼロ」([]) と区別する。初回の収集では両者とも空のヒットとして続けるが、
        判定失敗後の集め直しでは、集め損ねたときに最初の収集結果を捨てない
        (成功済みの候補を障害の空で上書きすると、粘着台帳が「候補の無かったターン」
        として古び、障害が続いただけで粘着記憶が消える)。
        """
        try:
            from sai_memory.unified_recall import unified_recall
            # Chronicle は自動想起の対象から外す。Chronicle は人生の背骨を要約した
            # 広い文なので意味ベクトルが多くの話題に薄く当たり、どんなクエリでも
            # cosine が高めに出て門を通りやすい (「ふと浮かぶ」より「常に浮かぶ」に
            # なる)。かつ要約ゆえ具体シーンが無く、会話に連想として繋げにくい
            # (fragment / message は「そのときの会話」があるので連想として振る舞える)。
            # 将来はソース種別ごとの「思い出しやすさ」に一般化する予定 (§4 参照)。
            return unified_recall(
                conn, embedder, query,
                topk=topk,
                search_chronicle=False,
                search_memopedia=True,
                search_fragments=True,
                search_messages=True,
                **extra_kwargs,
            )
        except Exception:
            LOGGER.warning("[auto_recall] unified_recall raised (persona=%s)", persona_id, exc_info=True)
            return None

    # 強化が効いているターンだけ乗せる追加引数。空のままなら従来の集め方そのもの。
    enhanced_kwargs: Dict[str, Any] = {}
    if not query:
        LOGGER.debug("[auto_recall] empty query; entity-trigger-only pass (persona=%s)", persona_id)
        hits = []
    else:
        if reflex_enabled:
            keywords = _extract_recall_keywords(conn, query, observations=observations)
            enhanced_kwargs = {
                # 抽出できなかったターンは None = 従来の split 挙動に戻す。
                "keywords": keywords or None,
                # ユーザーが今言った発話そのものは message 枠を毎ターン占領する
                # だけで、後段で「既にコンテキストにある」として捨てられる。
                # 収集段階で外して枠を空ける。
                "exclude_message_ids": context_ids,
                "source_allocations": _ENHANCED_SOURCE_ALLOCATIONS,
            }
        collected = _collect(enhanced_kwargs)
        hits = collected if collected is not None else []

    # --- 反射判断による選別 (オプション、既定 OFF) ---
    # None = 反射判断を使わない/使えない (既存の cosine しきい値方式で判定する)。
    # dict = 判定結果 (key -> noul 確率)。候補ゼロなら空 dict。
    reflex_decisions: Optional[Dict[Tuple[str, str], float]] = None
    # 「時間内に答えなかったせいで従来方式へ戻った」ターンの印 (画面の注記用)。
    reflex_deadline_fallback = False
    if reflex_enabled:
        reflex_decisions, reflex_deadline_fallback = _run_reflex_rerank(
            hits, messages,
            accepted_keys=accepted_keys, context_ids=context_ids, persona_id=persona_id,
            observations=observations, reflex_model_key=reflex_model_key,
        )
        if reflex_decisions is None and enhanced_kwargs:
            # 判定が使えなかったターンは、候補集めからやり直して従来の形に戻す。
            # 広げた母集団のまま cosine の選別に掛けると、従来経路では拾わない記憶が
            # 注入されたり、枠の違いで拾えたはずの記憶が落ちたりする — 「従来方式へ
            # 戻った」というログと実際の挙動が食い違う。
            LOGGER.info(
                "[auto_recall][reflex] the judgment was unavailable this turn; "
                "re-collecting candidates the conventional way (persona=%s)", persona_id,
            )
            recollected = _collect({})
            if recollected is not None:
                hits = recollected
            else:
                # 集め直し自体が失敗した回は、成功済みの広い候補のまま cosine 選別へ
                # (このターンだけ 6 巡目以前の受容済みの形に戻る)。空で上書きして
                # 粘着台帳を古びさせるより、実在の候補で選別する方が従来に近い。
                LOGGER.warning(
                    "[auto_recall][reflex] re-collection failed; keeping the enhanced "
                    "candidates for this turn's cosine selection (persona=%s)", persona_id,
                )

    # --- 選別: しきい値を満たすヒットを台帳に反映 ---
    for hit in hits:
        key = (hit.source_type, str(hit.source_id))
        # エンティティトリガーで既に採用済みなら埋め込み側は重複処理しない。
        if key in accepted_keys:
            continue
        # message ソースで、既にコンテキスト窓に載っているものは注入しない (二重注入回避)。
        if hit.source_type == "message" and str(hit.source_id) in context_ids:
            LOGGER.debug(
                "[auto_recall] drop msg already in context: id=%s embed=%.3f",
                hit.source_id, hit.embed_score if hit.embed_score is not None else -1.0,
            )
            continue

        embed_score = hit.embed_score
        # キーワードのみのヒット (embed_score is None) は採用しない。
        # 判断根拠は報告に記載 (キーワード一致は「今この話題に浮かぶべき記憶」の
        # 連想的近さを保証しない — RRF 融合ではブーストするが、自動想起は「勝手に
        # 浮かぶ」連想モデルなので埋め込み類似のみを採用の門にする)。
        if embed_score is None:
            LOGGER.debug(
                "[auto_recall] skip keyword-only hit: %s/%s (no embed_score)",
                hit.source_type, hit.source_id,
            )
            continue

        if reflex_decisions is not None:
            # 反射判断の経路: 採否は Noul 確率だけで決める。message ソースの底上げ
            # (長文類似度インフレへの対症療法) は直接判定が置き換えるので適用しない。
            # 判定が無いのは floor 未満で判断に渡していない候補だけ (判断層は全 qid が
            # 揃った応答しか成功にしないので、floor 以上の候補は必ず判定を持つ)。
            # 念のため dict.get の防御は残す。
            noul = reflex_decisions.get(key)
            passes = noul is not None and noul >= _REFLEX_THRESHOLD
            LOGGER.debug(
                "[auto_recall][reflex] %s/%s embed=%.3f noul=%s -> %s title=%r",
                hit.source_type, hit.source_id, embed_score,
                f"{noul:.3f}" if noul is not None else "(below floor; not judged)",
                "ACCEPT" if passes else "reject", (hit.title or "")[:40],
            )
        else:
            # message ソースだけしきい値を底上げする (長文類似度インフレ対策。上の
            # get_message_threshold_offset() docstring 参照)。
            effective_threshold = threshold + msg_threshold_offset if hit.source_type == "message" else threshold
            passes = embed_score >= effective_threshold
            LOGGER.debug(
                "[auto_recall] hit %s/%s embed=%.3f threshold=%.3f -> %s title=%r",
                hit.source_type, hit.source_id, embed_score, effective_threshold,
                "ACCEPT" if passes else "below", (hit.title or "")[:40],
            )
        if not passes:
            continue

        accepted_keys.add(key)
        accepted_count += 1
        existing = ledger.items.get(key)
        if existing is not None:
            existing.stale_turns = 0
            existing.best_score = max(existing.best_score, embed_score)
            # 表示材料は最新ヒットで更新 (content が伸びる/変わる場合に追従)。
            existing.title = hit.title or existing.title
            existing.content = hit.content or existing.content
            existing.uri = hit.uri or existing.uri
            if hit.source_type == "fragment":
                existing.chronicle_entry_id = hit.chronicle_entry_id or existing.chronicle_entry_id
        else:
            ledger.items[key] = _LedgerItem(
                source_type=hit.source_type,
                source_id=str(hit.source_id),
                title=hit.title or "",
                content=hit.content or "",
                uri=hit.uri or "",
                best_score=embed_score,
                stale_turns=0,
                chronicle_entry_id=hit.chronicle_entry_id if hit.source_type == "fragment" else None,
            )

    # --- 粘着: 今ターン採用されなかった既存アイテムの stale_turns を進める / 除去 ---
    to_remove: List[Tuple[str, str]] = []
    for key, item in ledger.items.items():
        if key in accepted_keys:
            continue
        item.stale_turns += 1
        if item.stale_turns > sticky_turns:
            to_remove.append(key)
    for key in to_remove:
        ledger.items.pop(key, None)
        LOGGER.debug("[auto_recall] evicted from ledger (stale): %s", key)

    if not ledger.items:
        LOGGER.debug("[auto_recall] ledger empty after update; no injection (persona=%s)", persona_id)
        # 注入が無いターンでも旗は持ち帰る — 判定が時間切れになった事実は、記憶が
        # 一つも浮かばなかったかどうかとは関係が無い (費用は同じく発生している)。
        return AutoRecallResult(
            False, None, query, len(hits), accepted_count, 0, 0,
            reflex_deadline_fallback=reflex_deadline_fallback,
        )

    # --- 注入ブロック組み立て: 台帳に残っているアイテムを全件注入する ---
    # 台帳に残っている = まだ sticky_turns 以内 (= 粘着中) なので、intent doc §4.3 の
    # sticky 仕様「一度浮かんだ記憶はしきい値を割っても解除されるまで注入を維持する」
    # に従い、全件を見せる。鮮度 (stale_turns 昇順) → スコア降順の並びは表示順のみの意味。
    #
    # 以前はここで注入ブロックの文字数上限 (char_budget) による足切りをしていたが、
    # それが粘着アイテムを削り落として「台帳には残っているのに LLM にも UI にも
    # 出ない幽霊」にする sticky 仕様違反バグだった (新規採用 stale=0 が先頭を占め、
    # 粘着 stale>=1 が末尾で毎ターン予算超過により消えていた)。浮かぶ「数」の抑制は
    # 取り込み側 (1ターンの新規採用数) の責務であって、表示側の予算足切りではない。
    ordered = sorted(
        ledger.items.values(),
        key=lambda it: (it.stale_turns, -it.best_score),
    )
    header = "ふと浮かんだ記憶:"
    included_items: List[_LedgerItem] = list(ordered)
    lines: List[str] = [_format_line(item) for item in included_items]

    # --- 鮮明想起 (§C, vivid recall) — rev4: 注入順序決定後の「全 fragment 項目」に
    #     「そのときの会話」サブ行を添える (先頭項目だけ最大2行、それ以外は1行 =
    #     鮮明度の勾配)。fragment + chronicle_entry_id を持つ項目のみ対象。LLM は
    #     呼ばない。vivid サブ行の合計は VIVID_CHARS を予算に並び順で消費し、尽きたら
    #     残りの項目はスキップする (これは vivid サブ行だけの上限。想起アイテム本体は
    #     §4.3 に従い常に全件出す)。
    vivid_lines_by_index: Dict[int, List[str]] = {}
    vivid_budget_remaining = get_vivid_char_budget()
    for idx, item in enumerate(included_items):
        if item.source_type != "fragment" or not item.chronicle_entry_id:
            continue
        if vivid_budget_remaining <= 0:
            LOGGER.debug(
                "[auto_recall][vivid] budget exhausted; skip remaining items from index %d (persona=%s)",
                idx, persona_id,
            )
            break
        max_messages = _VIVID_MAX_MESSAGES_TOP if idx == 0 else _VIVID_MAX_MESSAGES_REST
        vivid_messages = _vivid_messages_for_item(conn, item, max_messages=max_messages)
        if not vivid_messages:
            continue
        item_lines = _format_vivid_lines(vivid_messages, char_budget=vivid_budget_remaining)
        if not item_lines:
            LOGGER.debug(
                "[auto_recall][vivid] item %s/%s produced no lines within remaining budget %d",
                item.source_type, item.source_id, vivid_budget_remaining,
            )
            continue
        block_len = sum(len(ln) + 1 for ln in item_lines)
        vivid_lines_by_index[idx] = item_lines
        vivid_budget_remaining -= block_len
        LOGGER.debug(
            "[auto_recall][vivid] attached %d vivid line(s) to item[%d] %s/%s (+%d chars, remaining budget=%d)",
            len(item_lines), idx, item.source_type, item.source_id, block_len, vivid_budget_remaining,
        )

    # --- フッター行 (§B: 冗長排除) — 含まれたアイテムのハンドル union を1行だけ出す。
    footer = _format_footer(included_items)

    # 各行の直下に、その項目に割り当てられた vivid サブ行 (あれば) を挿入する。
    body_lines: List[str] = []
    for idx, line in enumerate(lines):
        body_lines.append(line)
        item_vivid = vivid_lines_by_index.get(idx)
        if item_vivid:
            body_lines.extend(item_vivid)
    body = header + "\n" + "\n".join(body_lines) + "\n" + footer
    block = f"<system>{body}</system>"

    LOGGER.info(
        "[auto_recall] injecting %d memories (persona=%s, thread=%s, %d chars, "
        "%d accepted this turn, ledger=%d, query=%r)",
        len(lines), persona_id, thread_id, len(block), accepted_count, len(ledger.items),
        query[:60],
    )

    return AutoRecallResult(
        injected=True,
        block=block,
        query=query,
        hit_count=len(hits),
        accepted_count=accepted_count,
        ledger_size=len(ledger.items),
        char_count=len(block),
        plain_text=body,
        reflex_deadline_fallback=reflex_deadline_fallback,
    )
