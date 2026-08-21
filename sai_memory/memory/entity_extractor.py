"""Extract entities and per-entity knowledge from conversation messages.

Replaces the old note_extractor + note_organizer pipeline.
Instead of extracting free-form notes and grouping them later,
this module identifies named entities (people, AIs, projects, etc.)
and extracts knowledge specific to each entity in a single LLM call.

The extracted entities are directly reflected in Memopedia:
- New entities get a new page created
- Existing entities get notes appended to their page

同じ LLM コールにもう一つ別の仕事が相乗りする (想起用タグ、B2 欄):
``involved_entities`` = 「この範囲が関与した既存ページの列挙。新情報が無くても、
その対象のための調べ物・作業を含む」。抽出 (新知識) とタグ付け (関与) は
**同じ継ぎ目に乗る別の仕事**で、生成後にタイトル → page_id の解決層を挟んでから
``chunk_page_edges`` の辺として記帳する。
正典: docs/intent/persona_cognition/recall_tags_and_track_reduction.md §9.3 /
docs/intent/autonomous_behavior_v3.md §13.6「B2 欄と辺の格納」。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from sai_memory.memory.recall_edges import (
    add_chunk_page_edge,
    init_chunk_page_edge_tables,
)
from sai_memory.memory.storage import Message
from sai_memory.memopedia.storage import CATEGORY_DEFS, category_keys

LOGGER = logging.getLogger(__name__)


class ExtractionFailed(RuntimeError):
    """抽出そのものが成立しなかった（LLM 障害・空応答・スキーマ違反）。

    「何も抽出するものが無かった」（正常な ``entities: []``）とは別物。前者は
    依存の障害なので付箋に貼って拾い直す対象、後者は成功した結果。両方を空
    リストで返していたため、LLM が落ちた回も「成功」として付箋が剥がされて
    いた（Sol レビュー 2026-08-06 F1）。
    """

# Valid Memopedia categories (レジストリから生成 — ハードコード禁止)
VALID_CATEGORIES: set = set(category_keys("extractable"))
DEFAULT_CATEGORY = "terms"

# Category root page IDs (レジストリから生成 — ハードコード禁止)
CATEGORY_ROOT_IDS: Dict[str, str] = {
    k: f"root_{k}" for k in category_keys("extractable")
}

#: 想起用タグ (``involved_entities``) の指し先にできるページのカテゴリ。
#: 「指し先は実体ページに限り自由語は許さない」(recall_tags §9.3) の実装で、
#: 抽出プロンプトが一覧で見せている範囲 (:func:`_format_page_list`) と同じに
#: 揃える —— 見せていない棚 (Chronicle・コア記憶・テーマ) を指し先にすると、
#: LLM が見ていないものへ辺が張れてしまう。
INVOLVEMENT_TARGET_CATEGORIES: tuple = tuple(category_keys("extractable"))


@dataclass
class ExtractedEntity:
    """An entity recognized from conversation with associated knowledge notes."""
    name: str
    category: str
    summary: str = ""
    notes: List[str] = field(default_factory=list)


@dataclass
class ExtractionOutput:
    """抽出コール 1 回ぶんの応答。同じコールに乗る二つの仕事を分けて持つ。

    - ``entities``: この会話で**新たに判明した知識** (Memopedia へ反映する)
    - ``involved_titles``: この範囲が**関与した既存ページのタイトル** (B2 欄)。
      新情報が無くても、その対象のための調べ物・作業をしていれば載る。
      ここではまだ生のタイトル文字列で、page_id への解決は
      :func:`_resolve_involved_page_ids` が別に行う。
    """

    entities: List["ExtractedEntity"] = field(default_factory=list)
    involved_titles: List[str] = field(default_factory=list)


@dataclass
class EntityExtractionResult:
    """Result of entity extraction and Memopedia reflection."""
    entity_name: str
    page_id: str
    is_new_page: bool
    notes_appended: int
    #: 同じ出所・同じ文が既にあったので作らなかった数。0 でない回は
    #: 「増えなかった」ことが呼び出し元から見えるようにする —— 重複検査が
    #: 黙って落としているように見せない (Codex 五巡 #2)
    notes_deduped: int = 0


def _build_extraction_prompt(
    conversation: str,
    *,
    episode_context: str = "",
    existing_pages: str = "",
) -> str:
    """Build the entity extraction prompt."""
    parts = [
        "あなたは知識ベースの整理係です。以下の会話に登場する固有の対象（エンティティ）を認識し、"
        "各エンティティについて新たに判明した情報を抽出してください。",
        "",
        "## エンティティとは",
        "",
        "Memopedia（知識ベース）でページを持つべき固有の対象物です。",
        "- 人物: ユーザー、友人、家族など（例: まはー、ナチュレ）",
        "- AI: AIペルソナ、AIプロジェクト（例: エイド、エリス）",
        "- プロジェクト/作品: ソフトウェア、ゲーム、創作物（例: SAIVerse、Project N.E.K.O.）",
        "- 用語/概念: 固有の技術用語、独自概念（例: Playbook、マザーAI法）",
        "- 場所: 固有名を持つ場所（例: 創造の祭壇）",
        "- イベント: 固有の出来事（例: ぽこあポケモン発売）",
        "",
        "**エンティティではないもの**:",
        "- 一般名詞や抽象概念",
        "- Wikipediaや検索エンジンで調べれば誰でも分かるもの"
        "（広く知られたサービス、技術用語、プログラミング言語、ツール名など）",
        "- ファイル名、パス、CLIコマンド名",
        "- IDやハッシュ値、ランダムな文字列、メールアドレス",
        "- APIスコープ名、認証トークン、パスワード等の技術的な識別子",
        "- その場限りのエラーメッセージやステータス",
        "",
        "**判定基準**: 「これは会話の当事者たちの間でだけ通じる固有の対象か？」",
        "世間一般に知られている事物は、この人たちの個人的な知識ベースに記録する必要はありません。",
        "当事者たちの関係性・創作・プロジェクトに固有の対象だけを記録してください。",
        "",
        "## 会話当事者についての注意",
        "",
        "会話の当事者（ペルソナ自身、ユーザー、頻繁に登場するAI）については、"
        "**6ヶ月後でも変わらない恒久的な属性のみ**を記録してください。",
        "",
        "記録すべき（恒久的な属性）:",
        "- 誕生日、好き嫌い、価値観、信念、性格的特徴",
        "- 人間関係や関係性の定義",
        "- 恒久的な習慣や癖",
        "",
        "記録すべきでない（行動記録・一時的な状態）:",
        "- 「〜した」「〜を行った」「〜を試みた」「〜に成功した」のような行動の記録",
        "- 「〜に直面している」「〜を感じている」のような一時的な状態",
        "- 作業の詳細や手順",
        "",
        "悪い例（行動記録であり、Chronicleの仕事）:",
        "- 「設定ファイルを編集して問題を解決した」",
        "- 「長時間の作業により疲労を感じている」",
        "",
        "良い例（恒久的な属性）:",
        "- 「几帳面な性格で、細部にこだわる傾向がある」",
        "- 「物語の中に驚きや成長の要素があることを好む」",
        "",
    ]

    if episode_context:
        parts.extend([
            "## これまでの流れ（参考）",
            episode_context,
            "",
        ])

    if existing_pages:
        parts.extend([
            "## 既存のMemopediaページ一覧",
            "以下のページが既に存在します。同名のエンティティが会話に登場した場合は、"
            "既に記録済みの情報と重複しないよう、新たに判明した情報のみを抽出してください。"
            "一覧にないエンティティが会話に登場した場合は新規として抽出してください。",
            existing_pages,
            "",
        ])

    parts.extend([
        "## 今回の会話",
        conversation,
        "",
        "## 指示",
        "",
        "- 会話に登場するエンティティ（固有名詞を持つ対象）を列挙してください",
        "- 各エンティティについて **summary**（そのエンティティが何であるかの一文定義）を書いてください",
        "  - 例: 「ソフィーの一人であるAIペルソナ」「まはーが開発しているAIプラットフォーム」",
        "  - 既存ページにあるエンティティでも、summaryは毎回書いてください",
        "- 各エンティティについて、この会話で新たに判明した事実・属性・状態変化を **notes** として記録してください",
        "- **notesは、その情報単体で意味が通じるように書いてください**。エンティティ名のページに記載される情報なので、",
        "  「このエンティティは何か」という前提知識がない読者でも理解できる文脈を含めてください",
        "  - 悪い例: 「元コードネームは「γ」である」（何の文脈か不明）",
        "  - 良い例: 「SAIVerseの開発初期にはコードネーム「γ」で呼ばれていた」",
        "- notesは短い1文で記述してください（「〜である」「〜が判明した」等）",
        "- 会話の中で明確に述べられた事実のみを抽出してください（推測や解釈は含めない）",
        "- 既存ページに記録済みの情報と重複するnotesは除外してください",
        "- 抽出すべきエンティティがない場合は空のリストを返してください",
        f"- categoryは {' / '.join(category_keys('extractable'))} のいずれかを指定してください",
        "- 日本語で出力してください",
        "",
        "## この会話が関与した対象（involved_entities）",
        "",
        "上の抽出とは**別の仕事**です。新しく判明した情報の有無に関係なく、"
        "この会話が関わった対象を列挙してください。",
        "",
        "- 「関わった」とは、その対象について話した／その対象のための調べ物・"
        "作業・準備をした、ということです",
        "- **新しく判明した情報が無くても列挙してください**。"
        "entities に挙がらなかった対象こそ、ここで拾う価値があります",
        "- 既存のMemopediaページ一覧にあるタイトルを、**一覧に書かれているとおりの"
        "表記で**書いてください（表記が違うと照合できず捨てられます）",
        "- 一覧に無い名前、説明的な言い回し、一般名詞は書かないでください",
        "- 関わった対象が無い場合は空のリストを返してください",
        "",
        "## 出力形式",
        "",
        "以下のJSON形式で出力してください。",
        "```json",
        json.dumps({
            "entities": [
                {
                    "name": "エンティティ名",
                    "category": "people",
                    "summary": "このエンティティが何であるかの一文定義",
                    "notes": ["文脈を含む事実1", "文脈を含む事実2"],
                },
            ],
            "involved_entities": ["既存ページのタイトル1", "既存ページのタイトル2"],
        }, ensure_ascii=False, indent=2),
        "```",
        "",
        "JSONのみを出力してください。",
    ])

    return "\n".join(parts)


def _format_messages(messages: List[Message]) -> str:
    """Format messages for the extraction prompt."""
    lines: List[str] = []
    for msg in messages:
        role = msg.role
        if role == "model":
            role = "assistant"
        content = (msg.content or "").strip()
        if not content:
            continue
        ts_str = datetime.fromtimestamp(msg.created_at).strftime("%Y-%m-%d %H:%M") if msg.created_at else "?"
        lines.append(f"[{ts_str}] [{role}]: {content}")
    return "\n\n".join(lines)


def _parse_extraction_response(response: str) -> ExtractionOutput:
    """Parse LLM response into an :class:`ExtractionOutput`.

    Raises:
        ExtractionFailed: JSON として読めない／期待した形をしていない応答。
            「抽出ゼロ」と区別できないまま空リストを返すと、失敗が成功として
            付箋から剥がれる。

    Note:
        ``involved_entities`` (B2 欄) の欠落や型不正は :class:`ExtractionFailed`
        にしない —— 抽出 (新知識) はそれ自体が成立しており、落ちた回をまるごと
        やり直させるほうが害が大きい。取りこぼしたタグは後から全記憶を走査して
        遡及できる (recall_tags §9.3 の保険) が、知識は拾い直しに失敗すれば
        永久に落ちる。非対称なのはこの理由による。
    """
    if not response:
        raise ExtractionFailed("entity extraction returned an empty response")

    text = response.strip()

    # Strip markdown code block if present
    md_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if md_match:
        text = md_match.group(1).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        LOGGER.warning("Failed to parse entity extraction response as JSON: %s", text[:300])
        raise ExtractionFailed(
            f"entity extraction response is not valid JSON: {exc}"
        ) from exc

    if isinstance(parsed, list):
        # 旧形式 (entities を包まない素の配列) の後方互換。この形には
        # involved_entities を載せる場所が無い
        entities_raw = parsed
        involved_titles: List[str] = []
    elif isinstance(parsed, dict) and "entities" in parsed:
        entities_raw = parsed["entities"]
        involved_titles = _parse_involved_titles(parsed.get("involved_entities"))
    else:
        # 指示した形 ({"entities": [...]}) でない = 抽出が成立していない。
        # 「entities キーはあるが空リスト」だけが正常な抽出ゼロ。
        raise ExtractionFailed(
            f"entity extraction response has no 'entities' field: {text[:200]}"
        )
    if not isinstance(entities_raw, list):
        # null や辞書は「抽出ゼロ」ではない。空リストへ読み替えると、壊れた応答が
        # 成功として付箋から剥がれる。
        raise ExtractionFailed(
            f"entity extraction 'entities' is not a list: {type(entities_raw).__name__}"
        )

    results = []
    for item in entities_raw:
        if not isinstance(item, dict):
            raise ExtractionFailed(
                f"entity extraction produced a non-object entity: {str(item)[:120]}"
            )
        name = _text_field(item, "name")
        if not name:
            # 名前のない項目はページの置き場所が決まらない。応答全体を捨てるほど
            # ではないので飛ばすが、黙って落とさない。
            LOGGER.warning(
                "Entity extraction: skipped an entity without a name: %s",
                str(item)[:200],
            )
            continue
        category = (_text_field(item, "category") or DEFAULT_CATEGORY).lower()
        if category not in VALID_CATEGORIES:
            category = DEFAULT_CATEGORY
        summary = _text_field(item, "summary")
        raw_notes = item.get("notes", [])
        if isinstance(raw_notes, str):
            # 1 件だけを素の文字列で返す LLM がいる。文字列をそのまま回すと
            # 一文字ずつ Fragment になるので、ここで 1 件のリストに読み替える。
            raw_notes = [raw_notes]
        if not isinstance(raw_notes, list):
            raise ExtractionFailed(
                f"entity extraction 'notes' is not a list: {type(raw_notes).__name__}"
            )
        notes = []
        for note in raw_notes:
            note_str = str(note).strip()
            if note_str:
                notes.append(note_str)
        if notes or summary:
            results.append(ExtractedEntity(name=name, category=category, summary=summary, notes=notes))

    return ExtractionOutput(entities=results, involved_titles=involved_titles)


def _parse_involved_titles(raw: Any) -> List[str]:
    """``involved_entities`` (B2 欄) を生のタイトル列として読む。

    欄そのものが無い／型が違うときは空リストを返す (:func:`_parse_extraction_response`
    の Note 参照)。要素単位の不正はその要素だけ捨てる —— タグは選別ではなく
    遡りの材料なので、1 件の型不正で残り全部を巻き添えにしない。
    """
    if raw is None:
        # 欄が無い応答 (旧プロンプトのモデル・欄を無視したモデル)。抽出は成立
        # しているので黙って通す —— 毎チャンク WARNING を出しても打つ手がない
        return []
    if isinstance(raw, str):
        # 1 件だけを素の文字列で返すモデルがいる (notes と同じ癖)。一文字ずつ
        # タイトルとして照合しにいかないよう、ここで 1 件のリストに読み替える
        raw = [raw]
    if not isinstance(raw, list):
        LOGGER.warning(
            "Entity extraction: 'involved_entities' is not a list (%s) — "
            "この回の想起用タグは記帳しません (抽出そのものは続行)",
            type(raw).__name__,
        )
        return []

    titles: List[str] = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            LOGGER.warning(
                "Entity extraction: involved_entities の要素が文字列ではないため"
                "捨てました: %s", str(item)[:120],
            )
            continue
        title = item.strip()
        if not title or title in seen:
            continue
        seen.add(title)
        titles.append(title)
    return titles


def _text_field(item: Dict[str, Any], key: str) -> str:
    """エンティティの文字列フィールドを取り出す (無ければ空文字)。

    Raises:
        ExtractionFailed: 文字列でも null でもない値。辞書やリストを ``str()``
            すると "['a']" のような文字列がページの要約に入り込む。
    """
    value = item.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ExtractionFailed(
            f"entity extraction field '{key}' is not a string: "
            f"{type(value).__name__}"
        )
    return value.strip()


def _format_page_list(memopedia) -> str:
    """Format existing Memopedia pages as a compact list for the prompt."""
    try:
        tree = memopedia.get_tree()
    except Exception:
        # 一覧を渡せないと「既存ページを知らない」抽出になり、同名ページが
        # 増えやすくなる。抽出自体は続けるが、黙って落とさない。
        LOGGER.warning(
            "Failed to list existing Memopedia pages for the extraction prompt",
            exc_info=True,
        )
        return ""

    lines = []
    for category_key in category_keys("extractable"):
        pages = tree.get(category_key, [])
        if not pages:
            continue
        lines.append(f"[{category_key}]")
        for page in pages:
            title = page.get("title", "?")
            lines.append(f"  - {title}")
            for child in page.get("children", []):
                lines.append(f"    - {child.get('title', '?')}")
    return "\n".join(lines)


def extract_entities_and_involvement(
    client,
    messages: List[Message],
    *,
    episode_context: str = "",
    existing_pages: str = "",
    persona_id: Optional[str] = None,
) -> ExtractionOutput:
    """1 回の LLM コールで、新知識の抽出と関与の列挙をまとめて取る。

    同じコールに乗る別々の仕事 (recall_tags §9.3):
    ``entities`` が新たに判明した知識、``involved_titles`` がこの範囲の関与した
    既存ページのタイトル (B2 欄)。入力は B2 欄を足す前と同じで、増えるのは
    出力の数百文字だけ。

    Args:
        client: LLM client.
        messages: Conversation messages to process.
        episode_context: Chronicle context for surrounding events.
        existing_pages: Formatted list of existing Memopedia pages.
        persona_id: Optional persona ID for usage tracking.

    Returns:
        :class:`ExtractionOutput`。空の ``entities`` は「抽出するものが無かった」
        ——処理する材料が無い場合も含む。

    Raises:
        ExtractionFailed: LLM 呼び出しの失敗・空応答・読めない応答。呼び出し元
            （executor / bands）が付箋に貼り、次の Metabolism が拾い直す。
    """
    if not messages:
        return ExtractionOutput()

    conversation = _format_messages(messages)
    if not conversation.strip():
        return ExtractionOutput()

    prompt = _build_extraction_prompt(
        conversation,
        episode_context=episode_context,
        existing_pages=existing_pages,
    )

    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
    except Exception as exc:
        LOGGER.error("Entity extraction LLM call failed: %s", exc)
        raise ExtractionFailed(f"entity extraction LLM call failed: {exc}") from exc

    if not response:
        raise ExtractionFailed("entity extraction LLM returned an empty response")

    output = _parse_extraction_response(response)
    LOGGER.info(
        "Extracted %d entities and %d involved titles from %d messages",
        len(output.entities), len(output.involved_titles), len(messages),
    )
    for ent in output.entities:
        LOGGER.debug("  Entity '%s' [%s]: %d notes", ent.name, ent.category, len(ent.notes))

    return output


def extract_entities(
    client,
    messages: List[Message],
    *,
    episode_context: str = "",
    existing_pages: str = "",
    persona_id: Optional[str] = None,
) -> List[ExtractedEntity]:
    """新知識の抽出だけを取る (:func:`extract_entities_and_involvement` の細口)。

    Memopedia の作り直しなど、Chronicle のチャンクに紐づかない呼び出し元が使う
    ——辺を張る先 (チャンクのページ id) が無い経路では、関与の列挙を受け取っても
    記帳できないため。
    """
    return extract_entities_and_involvement(
        client, messages,
        episode_context=episode_context,
        existing_pages=existing_pages,
        persona_id=persona_id,
    ).entities



def reflect_to_memopedia(
    entities: List[ExtractedEntity],
    memopedia,
    *,
    source_time: Optional[int] = None,
    chronicle_entry_id: Optional[str] = None,
    precondition: Optional[Callable[[], None]] = None,
) -> List[EntityExtractionResult]:
    """Reflect extracted entities into Memopedia pages and create Fragments.

    For each entity:
    - Search for existing page by title
    - If found, refresh its summary when changed and create Fragments from notes
    - If not found, create a new page and create Fragments from notes

    Notes are recorded as Fragments, not appended to page content (content is
    reserved for manual edits).

    適用は :meth:`Memopedia.apply_entity_notes` に任せる——探す・作る・書くを
    ばらばらに実行すると、途中で落ちたときに Fragment だけが残り、拾い直しが
    同じ知識を新しい UUID でもう一度挿す（Sol レビュー 2026-08-06 F3）。

    Args:
        entities: Extracted entities from extract_entities().
        memopedia: Memopedia instance.
        source_time: Timestamp used as the source date on created Fragments.
        chronicle_entry_id: ID of the Chronicle Lv-1 entry that triggered this extraction.
        precondition: 書き込みの直前 (トランザクションの中) で呼ばれる検査。
            例外を投げれば何も書かれない (:meth:`Memopedia.apply_entity_notes`)。

    Returns:
        List of results describing what was done.
    """
    from sai_memory.memopedia.core import EntityNotes

    date_str = datetime.fromtimestamp(source_time).strftime("%Y-%m-%d") if source_time else datetime.now().strftime("%Y-%m-%d")

    applied = memopedia.apply_entity_notes(
        [
            EntityNotes(
                title=entity.name,
                parent_id=CATEGORY_ROOT_IDS.get(
                    entity.category, CATEGORY_ROOT_IDS[DEFAULT_CATEGORY],
                ),
                summary=entity.summary,
                notes=entity.notes,
            )
            for entity in entities
        ],
        chronicle_entry_id=chronicle_entry_id,
        source_date=date_str,
        precondition=precondition,
    )

    results = []
    for item in applied:
        results.append(EntityExtractionResult(
            entity_name=item.title,
            page_id=item.page_id,
            is_new_page=item.is_new_page,
            notes_appended=item.fragments_created,
            notes_deduped=item.fragments_deduped,
        ))
        LOGGER.info(
            "%s page '%s' (%s): %d fragments created%s",
            "Created new" if item.is_new_page else "Updated",
            item.title, item.page_id, item.fragments_created,
            f" ({item.fragments_deduped} already recorded)" if item.fragments_deduped else "",
        )

    return results


# ---------------------------------------------------------------------------
# 想起用タグ (B2 欄) — タイトル → page_id の解決と、辺の記帳
#
# 生成時点の involved_entities はただの文字列で、表記ゆれを含みうる (粒度実験で
# 2 回実演された: 自由語彙の半角化 / 閉語彙でも周辺文脈の表記に引きずられる)。
# 指し先を実体ページに限り自由語を許さないため、ここで既存ページのタイトルと
# 照合して page_id へ落とす層を挟む (recall_tags §9.3)。
# ---------------------------------------------------------------------------


def _resolve_involved_page_ids(memopedia, titles: List[str]) -> List[str]:
    """関与タイトルを既存 Memopedia ページの id へ解決する (完全一致)。

    照合の厳密さは既存の抽出経路に合わせる —— :meth:`Memopedia.apply_entity_notes`
    が同名ページを探すのも ``find_page_by_title`` の完全一致で、こちらだけ曖昧に
    寄せると「ページは新規作成されたのにタグは別のページへ張られる」がありうる。
    前後の空白は :func:`_parse_involved_titles` が既に落としている。

    解決できなかったタイトルは**その 1 件だけ**捨てて WARNING に残す。タグは選別
    ではなく遡りの材料で、取りこぼしは後から全記憶を走査して遡及できる
    (recall_tags §9.3) —— 逆に曖昧照合で誤ったページへ張った辺は、遡りを毒する。

    Raises:
        Exception: ページの読み取り自体が失敗したときは、そのまま外へ出す。
            「読めなかった」を「無かった」と同じ扱いにすると、DB が壊れている
            回に**タグが 1 件も無い正常な回**の顔で通り過ぎる。
    """
    resolved: List[str] = []
    seen = set()
    for title in titles:
        page = None
        for category in INVOLVEMENT_TARGET_CATEGORIES:
            page = memopedia.find_by_title(title, category)
            if page is not None:
                break
        if page is None:
            LOGGER.warning(
                "[recall-tags] 関与タイトル '%s' に一致する実体ページが無いため"
                "辺を張りません (指し先は実体ページに限る)", title[:80],
            )
            continue
        if page.is_trunk:
            # カテゴリの棚そのもの (「人物」「用語」…)。実体ではないので指し先に
            # しない —— 全チャンクが棚へ繋がると、遡りの材料として役に立たない
            LOGGER.warning(
                "[recall-tags] 関与タイトル '%s' はカテゴリの棚のため辺を"
                "張りません", title[:80],
            )
            continue
        if page.id in seen:
            continue
        seen.add(page.id)
        resolved.append(page.id)
    return resolved


def _record_involvement_edges(
    conn: sqlite3.Connection,
    chronicle_page_id: str,
    entity_page_ids: List[str],
    *,
    db_lock: Optional[Any] = None,
    precondition: Optional[Callable[[], None]] = None,
) -> int:
    """解決済みのページ id をチャンクとの辺として記帳する。新しく張れた本数を返す。

    書き込みは冪等 (:func:`~sai_memory.memory.recall_edges.add_chunk_page_edge`)
    なので、拾い直しで同じ抽出をもう一度走らせても辺は重ならない。
    失敗は握り潰さない —— 呼び出し元 (executor / bands) が付箋に貼り、次の
    Metabolism が抽出ごと拾い直す。

    ``precondition`` (「いま書いてよい状態か」の検査) は**錠前を保持したまま、
    INSERT と同じトランザクションの中**で呼ぶ。検査だけ別の錠・別の commit で
    行うと、検査を通ってから INSERT が届くまでの隙に別の実行が取り置きを進め、
    失効した実行の辺が確定してしまう (Codex レビュー 2026-08-21 #1)。
    Memopedia 側 (:meth:`Memopedia.apply_entity_notes`) が同じ形をしているのに、
    辺だけ検査の外に出ている理由は無い。
    """
    if not entity_page_ids:
        return 0
    created = 0
    # 錠前は「渡されなければ配り所から引く」(sai_memory.db_locks) —— 検査と
    # INSERT を、同じ DB を触る他の書き手と同じ錠前の下で直列化する。
    # BEGIN IMMEDIATE は別プロセスとの競合しか止めない (db_locks の docstring)。
    if db_lock is None:
        from sai_memory.db_locks import lock_for
        db_lock = lock_for(conn)
    with db_lock:
        own_tx = not conn.in_transaction
        init_chunk_page_edge_tables(conn, commit=own_tx)
        if own_tx:
            conn.execute("BEGIN IMMEDIATE")
        try:
            if precondition is not None:
                precondition()
            for page_id in entity_page_ids:
                if page_id == chronicle_page_id:
                    # チャンク自身への辺。指し先を実体ページのカテゴリに絞って
                    # いる限り起きないが、起きたなら解決層かページのカテゴリが
                    # 壊れている —— 黙って捨てず声を出す
                    LOGGER.warning(
                        "[recall-tags] chronicle=%s が自分自身を指しています — "
                        "辺は張りません", chronicle_page_id[:12],
                    )
                    continue
                if add_chunk_page_edge(
                    conn, chronicle_page_id, page_id, commit=False,
                ):
                    created += 1
            if own_tx:
                conn.commit()
        except Exception:
            if own_tx:
                try:
                    conn.rollback()
                except Exception:
                    LOGGER.warning(
                        "[recall-tags] 辺の書き込みの rollback に失敗しました",
                        exc_info=True,
                    )
            raise
    return created


def record_involvement(
    conn: sqlite3.Connection,
    memopedia,
    chronicle_page_id: Optional[str],
    involved_titles: List[str],
    *,
    db_lock: Optional[Any] = None,
    precondition: Optional[Callable[[], None]] = None,
) -> int:
    """B2 欄を辺にするところまでを一続きで行う。新しく張れた本数を返す。

    チャンク (あらすじ) も実体も Memopedia ページなので、辺はページ id 同士。
    チャンク側の id は Chronicle エントリの id をそのまま使う —— Chronicle の
    エントリ 1 件は trunk ``root_chronicle`` 配下のページとして格納されており、
    ページ id は旧 arasuji entry の UUID をそのまま流用している
    (:mod:`sai_memory.arasuji.storage` の P3b 物理統合)。つまり
    ``chronicle_entry_id`` は既にチャンクのページ id そのもの。

    ``precondition`` は :func:`_record_involvement_edges` へそのまま渡す
    (書き込みと同じトランザクションの中で通す)。
    """
    if not involved_titles:
        return 0
    if not chronicle_page_id:
        # 辺を張る先が無い。タグは生成されたのに落とすので、黙って捨てない
        LOGGER.warning(
            "[recall-tags] 関与 %d 件を記帳できません — このチャンクの Chronicle "
            "ページ id が渡されていないため、辺を張る先がありません",
            len(involved_titles),
        )
        return 0

    page_ids = _resolve_involved_page_ids(memopedia, involved_titles)
    created = _record_involvement_edges(
        conn, chronicle_page_id, page_ids,
        db_lock=db_lock, precondition=precondition,
    )
    LOGGER.info(
        "[recall-tags] chronicle=%s: 関与 %d 件中 %d 件を解決、辺 %d 本を記帳",
        chronicle_page_id[:12], len(involved_titles), len(page_ids), created,
    )
    return created


def extract_and_reflect(
    client,
    conn: sqlite3.Connection,
    messages: List[Message],
    *,
    episode_context: str = "",
    persona_id: Optional[str] = None,
    chronicle_entry_id: Optional[str] = None,
    db_lock: Optional[Any] = None,
    precondition: Optional[Callable[[], None]] = None,
) -> List[EntityExtractionResult]:
    """Extract entities from messages and reflect them to Memopedia in one call.

    Convenience function that combines extract_entities_and_involvement() +
    reflect_to_memopedia() + record_involvement().

    同じ LLM コールに乗る二つ目の仕事 (想起用タグ) もここで着地する: B2 欄の
    タイトルを既存ページの id へ解決し、``chunk_page_edges`` へ辺として記帳する。
    **新知識がゼロでも辺は書く** —— 関与は新情報の有無と別だから (§9.3)。

    Args:
        client: LLM client.
        conn: Database connection (for Memopedia access).
        messages: Conversation messages to process.
        episode_context: Chronicle context for surrounding events.
        persona_id: Optional persona ID for usage tracking.
        chronicle_entry_id: ID of the Chronicle Lv-1 entry for Fragment linkage.
            想起用タグの辺ではチャンク側のページ id としても使う
            (:func:`record_involvement` 参照 — 同じ id である理由もそちら)。
        db_lock: SAIMemoryAdapter の ``_db_lock``。省いても Memopedia は DB
            ファイルの錠前を配り所から取る (``sai_memory.db_locks``) ので同じ
            ものになる —— 渡すのは、付箋の書き込みなど Memopedia を通らない
            経路でも同じ錠前を使うため
            (docs/issues/memopedia_writers_bypass_adapter_lock.md)。
        precondition: 書き込みの直前 (トランザクションの中) で呼ばれる検査。
            例外を投げれば何も書かれない。**Memopedia への反映
            (:func:`reflect_to_memopedia`) と辺の記帳
            (:func:`record_involvement`) の両方**で、それぞれの書き込みと同じ
            トランザクションの中から呼ばれる。

    Returns:
        List of results describing what was created/updated.

    Raises:
        ExtractionFailed: 抽出が成立しなかった
            (:func:`extract_entities_and_involvement` 参照)。
        Exception: ページの読み取りや辺の書き込みが失敗したときはそのまま外へ
            出す —— 握り潰して空成功にすると、呼び出し元が付箋を貼れず、この回の
            関与は誰にも拾い直されない。
    """
    from sai_memory.memopedia import Memopedia
    # テーブルの用意は Memopedia のコンストラクタが**ロックの内側で**行う。
    # ここで先に呼ぶと同じ commit がロック外で走る (Sol レビュー F5)。
    memopedia = Memopedia(conn, db_lock=db_lock)

    existing_pages = _format_page_list(memopedia)

    output = extract_entities_and_involvement(
        client, messages,
        episode_context=episode_context,
        existing_pages=existing_pages,
        persona_id=persona_id,
    )

    if (
        precondition is not None
        and not output.entities
        and not output.involved_titles
    ):
        # 何も書くものが無かった回。reflect も辺の記帳も走らないので、検査が
        # 一度も通らない —— 取り置きを取り戻された実行が「拾い直した」顔で
        # 成功を返さないよう、ここで一度だけ通す
        precondition()

    results: List[EntityExtractionResult] = []
    if output.entities:
        source_time = max((m.created_at for m in messages), default=None)
        results = reflect_to_memopedia(
            output.entities, memopedia,
            source_time=int(source_time) if source_time else None,
            chronicle_entry_id=chronicle_entry_id,
            precondition=precondition,
        )

    # 辺の記帳は Memopedia への反映の**後**。この回に新しく作られたページも
    # 関与の指し先になれる (関与しているのは事実で、ページの新旧は関係ない)。
    #
    # 新知識ゼロでも関与は起きる (「新情報が無くても、その対象のための調べ物・
    # 作業を含む」recall_tags §9.3) ので、entities の有無で打ち切らない。
    # ``precondition`` は reflect の成否に関わらず渡す —— reflect が自分の
    # 書き込みの中で検査しているのに、辺だけ免除される理由が無い。reflect を
    # 通ってから辺が届くまでの間にも取り置きは動きうる (Codex 2026-08-21 #1)。
    record_involvement(
        conn, memopedia, chronicle_entry_id, output.involved_titles,
        db_lock=db_lock, precondition=precondition,
    )

    return results


# ---------------------------------------------------------------------------
# 抽出失敗の付箋 (backlog)
#
# 抽出が発火するのは「そのチャンクが Chronicle として確定する瞬間」の一度きりで、
# 確定済みチャンクは再実行で冪等スキップされる — 失敗した抽出には二度と番が
# 回ってこない。だから失敗した entry id をここに貼っておき、次の Metabolism の
# 頭で source_ids からメッセージを読み直して拾い直す (まはー裁定 2026-08-06、
# docs/issues/memopedia_writers_bypass_adapter_lock.md)。
# ---------------------------------------------------------------------------

#: 拾い直しの上限。壊れたデータで毎晩 LLM 課金し続けないための天井。
#: 超えた付箋は剥がさず残す (黙って諦めない) — retry は WARN でスキップする。
MAX_EXTRACTION_RETRIES = 3

#: 取り置き (in_progress) を放置とみなす時間。拾い直しの途中でプロセスが落ちると
#: 取り置きのまま残り、そのままでは二度と番が回らない。拾い直し 1 枚は LLM 1 回
#: なので、これを超えて掛かることはない。
#:
#: ⚠ 取り戻しは**先の実行を止めない**。死んだと見なした側が実際には生きていて
#: 1 時間後に戻ってきた場合、両方が抽出を実行しうる (付箋の DELETE / UPDATE は
#: 版で弾かれるが、LLM コールと Memopedia への書き込みは弾かれない)。同じ内容
#: なら :func:`~sai_memory.memopedia.storage.fragment_exists` の重複検査が受け
#: 止める。取り戻しは WARN で必ず記録する。
STALE_CLAIM_SECONDS = 3600

#: 拾い直しの対象になる付箋の条件。取り置き中の付箋は他所が処理中なので飛ばすが、
#: 放置された取り置きは取り戻す。パラメータは (max_retries, 放置とみなす時刻)。
_CLAIMABLE_WHERE = (
    "attempts <= ? AND ("
    "  state = 'pending'"
    "  OR (state = 'in_progress' AND COALESCE(claimed_at, 0) < ?)"
    ")"
)


def init_extraction_backlog_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_extraction_backlog (
            entry_id   TEXT PRIMARY KEY,
            failed_at  INTEGER NOT NULL,
            attempts   INTEGER NOT NULL DEFAULT 1,
            state      TEXT NOT NULL DEFAULT 'pending',
            version    INTEGER NOT NULL DEFAULT 1,
            claimed_at INTEGER
        )
        """
    )
    # 先行版 (2026-08-06) の付箋には state / version / claimed_at が無い。
    existing = {row[1] for row in conn.execute(
        "PRAGMA table_info(entity_extraction_backlog)"
    )}
    for column, ddl in (
        ("state", "TEXT NOT NULL DEFAULT 'pending'"),
        ("version", "INTEGER NOT NULL DEFAULT 1"),
        ("claimed_at", "INTEGER"),
    ):
        if column not in existing:
            conn.execute(
                f"ALTER TABLE entity_extraction_backlog ADD COLUMN {column} {ddl}"
            )
    conn.commit()


def record_extraction_failure(
    conn: sqlite3.Connection,
    entry_id: str,
    *,
    db_lock: Optional[Any] = None,
) -> None:
    """抽出の失敗を付箋に貼る (既にあれば失敗回数を重ねる)。

    版 (``version``) を必ず進める。拾い直し中の付箋にこれが重なったとき、
    先に取り置いた側の「成功したから剥がす」が版のずれで空振りし、**後から
    来た新しい失敗が消されない**（Sol レビュー 2026-08-06 F2）。
    """
    with (db_lock or nullcontext()):
        init_extraction_backlog_table(conn)
        conn.execute(
            "INSERT INTO entity_extraction_backlog "
            "  (entry_id, failed_at, attempts, state, version, claimed_at) "
            "VALUES (?, ?, 1, 'pending', 1, NULL) "
            "ON CONFLICT(entry_id) DO UPDATE SET "
            "  attempts = attempts + 1, failed_at = excluded.failed_at, "
            "  state = 'pending', version = version + 1, claimed_at = NULL",
            (entry_id, int(time.time())),
        )
        conn.commit()


def count_extraction_backlog(
    conn: sqlite3.Connection,
    *,
    max_retries: int = MAX_EXTRACTION_RETRIES,
    db_lock: Optional[Any] = None,
) -> Dict[str, int]:
    """付箋の枚数を数える。

    Metabolism の頭で「拾い直しに入るか」「止まっていることを知らせるか」を
    決めるのに使う。

    Returns:
        ``{"claimable": n, "total": n}``。``claimable`` は**いま拾い直せる**枚数
        (上限超え・他所が処理中のものを除く)。0 なら LLM クライアントを用意する
        必要もない。``total`` は残っている付箋の全部で、上限超えで止まっている
        ものも含む —— claimable だけを見ると「上限で止まったまま永久に残る付箋」
        が 0 件に見えて、運用側から消える (Codex 四巡 #4)。
    """
    with (db_lock or nullcontext()):
        init_extraction_backlog_table(conn)
        claimable = conn.execute(
            f"SELECT COUNT(*) FROM entity_extraction_backlog WHERE {_CLAIMABLE_WHERE}",
            (max_retries, int(time.time()) - STALE_CLAIM_SECONDS),
        ).fetchone()
        total = conn.execute(
            "SELECT COUNT(*) FROM entity_extraction_backlog"
        ).fetchone()
    return {
        "claimable": int(claimable[0]) if claimable else 0,
        "total": int(total[0]) if total else 0,
    }


def _claim_backlog_entry(
    conn: sqlite3.Connection,
    entry_id: str,
    version: int,
    *,
    max_retries: int,
    db_lock: Optional[Any] = None,
    was_in_progress: bool = False,
) -> bool:
    """付箋 1 枚を取り置く。読んだときの版と一致したときだけ成立する。

    **取り置きの成立と同時に回数を数える。** 失敗したときだけ数えていると、
    処理の途中でプロセスが落ちた付箋は回数が増えないまま毎時間取り戻され、
    上限 (``max_retries``) が効かずに LLM を呼び続ける (Codex 二巡 #2)。
    """
    now = int(time.time())
    if was_in_progress:
        # 放置された取り置きの取り戻し。先の実行を止める手立ては無いので
        # (STALE_CLAIM_SECONDS 参照)、起きたことを必ず記録する。
        LOGGER.warning(
            "[extraction-backlog] entry=%s の取り置きが %d 秒以上動いていない"
            "ため取り戻します — 先の実行が生きていた場合、抽出が二重に走ります",
            entry_id[:8], STALE_CLAIM_SECONDS,
        )
    with (db_lock or nullcontext()):
        cur = conn.execute(
            "UPDATE entity_extraction_backlog "
            "SET state = 'in_progress', version = version + 1, "
            "    attempts = attempts + 1, claimed_at = ? "
            f"WHERE entry_id = ? AND version = ? AND {_CLAIMABLE_WHERE}",
            (now, entry_id, version, max_retries, now - STALE_CLAIM_SECONDS),
        )
        conn.commit()
    return cur.rowcount == 1


class ClaimLost(RuntimeError):
    """取り置きが自分のものでなくなった（別の実行に取り戻された）。

    :func:`_still_ours` が投げる。書き込みトランザクションの中で投げるので、
    この実行の書き込みは一つも入らない。
    """


def _still_ours(
    conn: sqlite3.Connection, entry_id: str, version: int,
) -> Callable[[], None]:
    """「この付箋の取り置きがまだ自分のものか」を確かめる検査を作る。

    時間の掛かった拾い直しは、戻ってきたときには取り置きを取り戻されている
    ことがある (:data:`STALE_CLAIM_SECONDS`)。版の検査を**書き込みと同じ
    トランザクションの中**で行い、自分のものでなければ書かせない —— 後片付け
    の版検査だけでは、確定してしまった書き込みは戻せない (Codex 三巡 #1)。
    """
    def check() -> None:
        row = conn.execute(
            "SELECT version FROM entity_extraction_backlog WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        # 行が消えている = 別の実行が成功して剥がした後。これも自分の番ではない。
        if row is None or row[0] != version:
            raise ClaimLost(
                f"backlog claim for {entry_id[:8]} is no longer ours "
                f"(expected version {version}, found {row[0] if row else 'none'})"
            )
    return check


def _release_backlog_entry(
    conn: sqlite3.Connection,
    entry_id: str,
    version: int,
    *,
    outcome: str,
    db_lock: Optional[Any] = None,
) -> bool:
    """取り置いた付箋を片づける。版が一致しなければ何もしない。

    版が動いているのは「拾い直している間に新しい失敗が上書きした」場合で、
    そのときは剥がしても状態を戻してもいけない（新しい失敗を消してしまう）。

    回数 (``attempts``) はここでは触らない —— 取り置きの成立と同時に数えて
    いる (:func:`_claim_backlog_entry`)。

    Args:
        outcome: ``"recovered"`` (成功したので剥がす) / ``"dropped"``
            (拾いようがないので剥がす) / ``"failed"`` (次の回へ戻して残す)。
    """
    with (db_lock or nullcontext()):
        if outcome == "failed":
            cur = conn.execute(
                "UPDATE entity_extraction_backlog "
                "SET state = 'pending', version = version + 1, "
                "    claimed_at = NULL, failed_at = ? "
                "WHERE entry_id = ? AND version = ?",
                (int(time.time()), entry_id, version),
            )
        else:
            cur = conn.execute(
                "DELETE FROM entity_extraction_backlog "
                "WHERE entry_id = ? AND version = ?",
                (entry_id, version),
            )
        conn.commit()
    if cur.rowcount != 1:
        LOGGER.info(
            "[extraction-backlog] entry=%s の後片付け (%s) は見送り — "
            "拾い直しの間に新しい失敗が貼り直されています",
            entry_id[:8], outcome,
        )
        return False
    return True


def retry_extraction_backlog(
    conn: sqlite3.Connection,
    callback: Callable[[List[Message], Optional[str]], None],
    *,
    max_retries: int = MAX_EXTRACTION_RETRIES,
    db_lock: Optional[Any] = None,
) -> Dict[str, int]:
    """付箋の拾い直し。次の Metabolism の頭で呼ぶ。

    entry の source_ids からメッセージを読み直し、確定時と同じ callback
    (:func:`make_batch_callback` の戻り) で抽出をやり直す。成功したら付箋を
    剥がす。失敗したら回数を重ね、``max_retries`` を超えた付箋は WARN で
    スキップ (行は残す — 黙って諦めない)。

    付箋は 1 枚ずつ**取り置いて**から処理する。取り置きに失敗した付箋は他所が
    処理中なので飛ばす —— 同じチャンクを二重に抽出して LLM を二重に呼ぶことも、
    処理中に貼り直された新しい失敗を消すこともない。``db_lock`` は付箋への
    書き込みだけで取る（LLM を呼ぶ callback は錠を持たずに走らせる）。

    LLM コールは付箋 1 枚につき 1 回。付箋の数は「一度は確定と共に承認された
    抽出のうち失敗した分」を超えないので、承認済みの範囲の拾い直しに収まる。

    Returns:
        ``{"recovered": n, "failed": n, "dropped": n, "exhausted": n,
        "skipped": n}``。dropped は entry かメッセージが消えていて拾いようが
        なかった付箋、skipped は他所が処理中で触らなかった付箋。
    """
    from sai_memory.memory.storage import get_messages_by_ids

    with (db_lock or nullcontext()):
        init_extraction_backlog_table(conn)
        rows = conn.execute(
            "SELECT entry_id, attempts, version, state FROM entity_extraction_backlog "
            "ORDER BY failed_at ASC, entry_id ASC"
        ).fetchall()

    stats = {"recovered": 0, "failed": 0, "dropped": 0, "exhausted": 0, "skipped": 0}
    for entry_id, attempts, version, state in rows:
        if attempts > max_retries:
            stats["exhausted"] += 1
            LOGGER.warning(
                "[extraction-backlog] entry=%s は %d 回失敗して上限超え — "
                "拾い直しを止めています (付箋は残る)",
                entry_id[:8], attempts,
            )
            continue
        if not _claim_backlog_entry(
            conn, entry_id, version, max_retries=max_retries, db_lock=db_lock,
            was_in_progress=(state == "in_progress"),
        ):
            stats["skipped"] += 1
            continue
        claimed_version = version + 1

        with (db_lock or nullcontext()):
            row = conn.execute(
                "SELECT source_ids_json FROM arasuji_entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
            source_ids = []
            source_broken = False
            if row is not None:
                try:
                    source_ids = json.loads(row[0] or "[]")
                except (TypeError, ValueError):
                    # 出所の記録が壊れている。「entry が消えた」とは別物 ——
                    # 元メッセージは在るかもしれないのに、辿る鍵だけが読めない。
                    # 剥がすとその範囲の知識が永久に失われる (Codex 八巡 #3)
                    source_broken = True
            messages = get_messages_by_ids(conn, source_ids) if source_ids else []

        if source_broken:
            _release_backlog_entry(
                conn, entry_id, claimed_version,
                outcome="failed", db_lock=db_lock,
            )
            stats["failed"] += 1
            LOGGER.error(
                "[extraction-backlog] entry=%s の出所の記録 (source_ids_json) が"
                "読めません — 付箋は残します (剥がすとこの範囲は永久に失われる)",
                entry_id[:8],
            )
            continue
        if not messages:
            # entry が消えたか元メッセージが辿れない — 拾いようがないので剥がす
            _release_backlog_entry(
                conn, entry_id, claimed_version,
                outcome="dropped", db_lock=db_lock,
            )
            stats["dropped"] += 1
            LOGGER.warning(
                "[extraction-backlog] entry=%s の元メッセージを辿れないため"
                "付箋を剥がしました", entry_id[:8],
            )
            continue

        # 元メッセージが**一部だけ**欠けている場合。残った分で抽出して成功に
        # すると、欠けた分の知識が「拾い直した」顔で永久に落ちる。LLM も呼ばず
        # 失敗として残す —— 消えたメッセージは戻らないので、回数を重ねてやがて
        # 上限で止まり、付箋は見えたまま残る (Codex 七巡 #2。同じ欠陥を
        # bands 側で直したとき、この経路を検算していなかった)
        missing = set(source_ids) - {m.id for m in messages}
        if missing:
            _release_backlog_entry(
                conn, entry_id, claimed_version,
                outcome="failed", db_lock=db_lock,
            )
            stats["failed"] += 1
            LOGGER.error(
                "[extraction-backlog] entry=%s の元メッセージが %d/%d 件"
                "失われています — 残りだけで抽出すると欠けた分が拾い直した顔で"
                "落ちるため、この付箋は成功にしません",
                entry_id[:8], len(missing), len(set(source_ids)),
            )
            continue

        try:
            callback(messages, entry_id, precondition=_still_ours(
                conn, entry_id, claimed_version,
            ))
        except ClaimLost:
            # 取り置きが他所へ移っていた (時間が掛かりすぎて取り戻された)。
            # 書き込みは何も入っていない。状態には触らない —— いま持っているのは
            # 別の実行なので、その邪魔をしない
            stats["skipped"] += 1
            LOGGER.warning(
                "[extraction-backlog] entry=%s の拾い直しは書き込み直前で取り下げ"
                "ました (取り置きが他所へ移っています)", entry_id[:8],
            )
            continue
        except Exception:
            stats["failed"] += 1
            LOGGER.exception(
                "[extraction-backlog] entry=%s の拾い直しに失敗 (attempts=%d)",
                entry_id[:8], attempts,
            )
            try:
                _release_backlog_entry(
                    conn, entry_id, claimed_version,
                    outcome="failed", db_lock=db_lock,
                )
            except Exception:
                # 後片付けにも失敗した = 付箋は取り置きのまま残る。次の
                # Metabolism では取り置き中として飛ばされ、放置とみなされる
                # までの時間が経つまで番が回らない。「次回もう一度」ではない
                # ことをそのまま書く (Codex 六巡 #5)
                LOGGER.error(
                    "[extraction-backlog] entry=%s の後片付けにも失敗しました "
                    "— 付箋は取り置きのまま残ります。次に拾い直されるのは "
                    "%d 秒後 (放置とみなされてから) です",
                    entry_id[:8], STALE_CLAIM_SECONDS, exc_info=True,
                )
            continue
        _release_backlog_entry(
            conn, entry_id, claimed_version,
            outcome="recovered", db_lock=db_lock,
        )
        stats["recovered"] += 1
    if any(stats.values()):
        LOGGER.info("[extraction-backlog] 拾い直し結果: %s", stats)
    return stats


def make_batch_callback(
    client,
    conn: sqlite3.Connection,
    *,
    persona_id: Optional[str] = None,
    db_lock: Optional[Any] = None,
) -> Callable[[List[Message], Optional[str]], None]:
    """Create a batch callback for Chronicle generation.

    Returns a callback that can be passed to
    sai_memory.arasuji.executor.execute_plan() as batch_callback. Each time a
    chunk of messages is compiled into Chronicle, this callback extracts
    entities and reflects them to Memopedia.

    Args:
        client: LLM client for entity extraction.
        conn: Database connection.
        persona_id: Optional persona ID for usage tracking.
        db_lock: SAIMemoryAdapter の ``_db_lock`` (``extract_and_reflect`` 参照)。

    Returns:
        Callback function accepting
        ``(batch_messages, chronicle_entry_id, precondition=None)``。
        ``precondition`` は Memopedia へ書く直前 (トランザクションの中) に呼ばれる
        検査で、拾い直し (:func:`retry_extraction_backlog`) が「この結果を書いて
        よいか」を確かめるために渡す。

    Note:
        失敗はここで握り潰さない。ペルソナの記憶追記が黙って落ちる穴だった
        (docs/issues/memopedia_writers_bypass_adapter_lock.md)。例外はそのまま
        呼び出し元 (executor.execute_plan) へ届き、あちらが「どのチャンクの
        抽出が失敗したか」を ExecutionResult に記録する。
    """
    def callback(
        batch_messages: List[Message],
        chronicle_entry_id: Optional[str] = None,
        precondition: Optional[Callable[[], None]] = None,
    ) -> None:
        if not batch_messages:
            return

        from sai_memory.arasuji.context import get_episode_context_for_timerange
        start_time = min(m.created_at for m in batch_messages)
        end_time = max(m.created_at for m in batch_messages)

        ep_ctx = ""
        try:
            ep_ctx = get_episode_context_for_timerange(
                conn, start_time=start_time, end_time=end_time, max_entries=10,
            )
        except Exception:
            pass

        results = extract_and_reflect(
            client, conn, batch_messages,
            episode_context=ep_ctx,
            persona_id=persona_id,
            chronicle_entry_id=chronicle_entry_id,
            db_lock=db_lock,
            precondition=precondition,
        )

        if results:
            new_count = sum(1 for r in results if r.is_new_page)
            update_count = sum(1 for r in results if not r.is_new_page)
            total_notes = sum(r.notes_appended for r in results)
            LOGGER.info(
                "Entity extraction batch complete: %d entities (%d new, %d updated), "
                "%d notes total, chronicle_entry=%s",
                len(results), new_count, update_count, total_notes,
                chronicle_entry_id[:12] if chronicle_entry_id else None,
            )

    return callback
