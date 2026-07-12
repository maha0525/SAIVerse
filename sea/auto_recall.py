"""自動想起 第0層 (ゾーン C) — 埋め込み検索による「浮かんだ記憶」の末尾注入。

記憶アーキテクチャ v2 (docs/intent/memory_architecture_v2.md) §4 の中核。ペルソナに
「思い出す」という行動を選ばせるのではなく、世界側が現在の会話に関連する記憶を
ローカル埋め込み検索で自動的に浮かび上がらせ、``<system>`` タグ付き user メッセージ
として履歴末尾 (最新入力の直前) に**一時注入**する。

不変条件 (§10):
- **LLM は絶対に呼ばない** (§10-1)。埋め込み検索 + 決定論フィルタのみ。
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
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

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
    content = msg.get("content")
    return isinstance(content, str) and bool(content.strip())


def build_query(messages: List[Dict[str, Any]], *, n: Optional[int] = None) -> str:
    """直近 n 件の会話本文を連結してクエリ文字列にする。

    埋め込みの ``query:`` プレフィックス付与は unified_recall/embedder 側 (is_query=True)
    が行うので、ここでは生テキストの連結だけを返す。
    """
    if n is None:
        n = get_query_message_count()
    convo = [m for m in messages if _is_conversational_message(m)]
    tail = convo[-n:] if n > 0 else convo
    return "\n".join(str(m.get("content", "")).strip() for m in tail).strip()


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


def run_auto_recall(
    conn,
    embedder,
    messages: List[Dict[str, Any]],
    *,
    persona_id: str,
    thread_id: str,
) -> AutoRecallResult:
    """自動想起を 1 ターン分実行し、注入ブロック (あれば) を返す。

    LLM は呼ばない。呼び出し側はスコープ (CONVERSATION アスペクト) を確認済みで
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

    Returns:
        AutoRecallResult。``injected`` が True のとき ``block`` を末尾注入する。
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

    if not query:
        LOGGER.debug("[auto_recall] empty query; entity-trigger-only pass (persona=%s)", persona_id)
        hits = []
    else:
        try:
            from sai_memory.unified_recall import unified_recall
            # Chronicle は自動想起の対象から外す。Chronicle は人生の背骨を要約した
            # 広い文なので意味ベクトルが多くの話題に薄く当たり、どんなクエリでも
            # cosine が高めに出て門を通りやすい (「ふと浮かぶ」より「常に浮かぶ」に
            # なる)。かつ要約ゆえ具体シーンが無く、会話に連想として繋げにくい
            # (fragment / message は「そのときの会話」があるので連想として振る舞える)。
            # 将来はソース種別ごとの「思い出しやすさ」に一般化する予定 (§4 参照)。
            hits = unified_recall(
                conn, embedder, query,
                topk=topk,
                search_chronicle=False,
                search_memopedia=True,
                search_fragments=True,
                search_messages=True,
            )
        except Exception:
            LOGGER.warning("[auto_recall] unified_recall raised (persona=%s)", persona_id, exc_info=True)
            hits = []

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
        return AutoRecallResult(False, None, query, len(hits), accepted_count, 0, 0)

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
    )
