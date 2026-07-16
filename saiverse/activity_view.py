"""ライフビュー (Persona Activity View) のバックエンドロジック。

ペルソナの自律行動を「見たければいつでも垣間見られる」ようにする観察面の
データ整形を担う。表示は既存データ (Track / pulse_logs / messages) の
テンプレート整形のみで作り、観察のために LLM は呼ばない (不変条件 2)。

責務:
- 自律 Pulse 間隔の実効値解決 (Track metadata > ペルソナ設定 > Handler デフォルト)
- Pulse ログから「最近」ダイジェスト 1 行 (生活の言葉) を組み立てる純粋関数群

責務外:
- API ルーティング / DB アクセス (api/routes/people/activity.py が担う)
- Pulse の発火制御 (旧 pulse_scheduler.py は自律行動 v2 で廃止。駆動は
  day_plan のコマ発火 + 判断点)

詳細: docs/intent/persona_activity_view.md
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger(__name__)

# META_JUDGMENT_CONFIG 上の「作業のテンポ」キー (persona_activity_view.md §7)。
AUTONOMOUS_PULSE_INTERVAL_KEY = "autonomous_pulse_interval_seconds"


def resolve_autonomous_pulse_interval(
    track_metadata: Optional[Dict[str, Any]],
    persona_config: Optional[Dict[str, Any]],
    handler_default: int = 30,
) -> int:
    """自律 Track の実効 Pulse 間隔 (秒) を解決する。

    優先順: Track metadata の ``pulse_interval_seconds`` > ペルソナ設定
    (META_JUDGMENT_CONFIG の ``autonomous_pulse_interval_seconds``) >
    Handler クラスデフォルト。

    0 は「毎 poll 連続実行」を意味する有効値 (既存のスケジューラ仕様)。
    負値のみ 0 に丸める。
    """
    if isinstance(track_metadata, dict):
        value = track_metadata.get("pulse_interval_seconds")
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                pass
    if isinstance(persona_config, dict):
        value = persona_config.get(AUTONOMOUS_PULSE_INTERVAL_KEY)
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                pass
    return max(0, int(handler_default))


# ----------------------------------------------------------------------
# 「最近」ダイジェスト: pulse_logs → 生活の言葉 1 行
# ----------------------------------------------------------------------

# ツール名 → (文テンプレート, 引数として表示するキーの優先リスト)。
# テンプレートの {arg} は引数値 (見つからなければ arg なしテンプレートに退避)。
# ここに無いツールは _GENERIC_TOOL_PHRASE で汎用表示する (アドオンツール等)。
_TOOL_PHRASES: Dict[str, Tuple[str, str, List[str]]] = {
    # (arg ありテンプレート, arg なしテンプレート, arg キー候補)
    "searxng_search": ("Web で「{arg}」を調べた", "Web で調べものをした", ["query"]),
    "read_url_content": ("Web ページを読んだ ({arg})", "Web ページを読んだ", ["url"]),
    "read_url_outline": ("Web ページの構成を確かめた ({arg})", "Web ページの構成を確かめた", ["url"]),
    "read_url_section": ("Web ページを読み進めた ({arg})", "Web ページを読み進めた", ["url"]),
    "pdf_read": ("PDF「{arg}」を読んだ", "PDF を読んだ", ["file_path", "path"]),
    "memory_recall": ("「{arg}」について記憶を探った", "記憶を探った", ["query"]),
    "memory_recall_unified": ("「{arg}」について記憶を探った", "記憶を探った", ["query"]),
    "memory_search_brief": ("「{arg}」について記憶を探った", "記憶を探った", ["query"]),
    "memory_read_around": ("過去の会話を読み返した", "過去の会話を読み返した", []),
    "chronicle_search": ("「{arg}」について過去の記録をたどった", "過去の記録をたどった", ["query"]),
    "chronicle_read_detail": ("過去の記録を読み返した", "過去の記録を読み返した", []),
    # 旧名 (過去ログ互換のため残す。新規発話は memory_* / purpose_* に統一済み)
    "memopedia_search": ("Memopedia で「{arg}」を調べた", "Memopedia を調べた", ["query"]),
    "memopedia_get_page": ("Memopedia「{arg}」を開いた", "Memopedia のページを開いた", ["title", "page_id"]),
    "memopedia_save_page": ("Memopedia「{arg}」を書いた", "Memopedia に書き込んだ", ["title"]),
    "memopedia_note": ("Memopedia にメモを残した", "Memopedia にメモを残した", []),
    "memopedia_edit_fragment": ("Memopedia の断片を整理した", "Memopedia の断片を整理した", []),
    # Memory Atlas 統一スペル (concept_consolidation.md P2)
    "memory_read": ("記憶のページ（{arg}）を読んだ", "記憶のページを読んだ", ["ref"]),
    "memory_open": ("記憶のページ（{arg}）を机に開いた", "記憶のページを机に開いた", ["ref"]),
    "memory_close": ("記憶のページ（{arg}）を机から閉じた", "記憶のページを机から閉じた", ["ref"]),
    "memory_search": ("「{arg}」について地図帳を調べた", "地図帳を調べた", ["query"]),
    "memory_write": ("記憶のページ（{arg}）に書いた", "記憶のページに書いた", ["title", "ref"]),
    "memory_clip": ("会話の一場面を切り出した", "会話の一場面を切り出した", []),
    "memory_delete": ("記憶のページ（{arg}）をごみ箱に移した", "記憶のページをごみ箱に移した", ["ref"]),
    # 目的の地図 (purpose_*) 統一スペル
    "purpose_seed": ("「{arg}」をやりたいことに書き留めた", "やりたいことを書き留めた", ["title"]),
    "purpose_adopt": ("「{arg}」を目的の木に接いだ", "目的の木に接いだ", ["title", "candidate_ref"]),
    "purpose_step": ("目的のステップを更新した", "目的のステップを更新した", []),
    "purpose_close": ("目的を閉じた", "目的を閉じた", []),
    "purpose_decompose": ("目的をステップに分解した", "目的をステップに分解した", []),
    "document_create": ("文書「{arg}」を書いた", "文書を書いた", ["title", "name"]),
    "document_edit": ("文書「{arg}」を編集した", "文書を編集した", ["title", "name"]),
    "document_read": ("文書「{arg}」を読んだ", "文書を読んだ", ["title", "name"]),
    "document_search": ("文書から「{arg}」を探した", "文書を探した", ["query"]),
    # 旧名 (過去ログ互換のため残す。note_create/note_search スペル自体は
    # P3c① (concept_consolidation.md「Note → テーマノード移行」) で退役済み —
    # 後継は memory_write / memory_search)
    "note_create": ("Note「{arg}」を作った", "Note を作った", ["title"]),
    "note_search": ("Note から「{arg}」を探した", "Note を探した", ["query"]),
    "image_generator": ("画像を生成した", "画像を生成した", []),
    "generate_image_local": ("画像を生成した", "画像を生成した", []),
    "calculator": ("計算をした", "計算をした", []),
    "schedule_add": ("予定「{arg}」を追加した", "予定を追加した", ["title", "content"]),
    "schedule_list": ("予定を確認した", "予定を確認した", []),
    "send_email_to_user": ("ユーザーにメールを送った", "ユーザーにメールを送った", []),
    "update_working_memory": ("考えを書き留めた", "考えを書き留めた", []),
    "item_view": ("アイテムを眺めた", "アイテムを眺めた", []),
    "item_annotate": ("アイテム「{arg}」を整理した", "アイテムを整理した", ["name"]),
    "move_persona": ("別の場所へ移動した", "別の場所へ移動した", []),
    "invoke_phenomenon": ("フェノメナを起こした", "フェノメナを起こした", []),
}

# ダイジェストに出さない内部ブックキーピング系ツール。これらしか無い Pulse は
# 汎用ラベル (「〜を進めた」) に落とす。
_HIDDEN_TOOLS = frozenset({
    "track_create", "track_pause", "track_activate", "track_complete",
    "track_abort", "track_list", "task_add", "task_done",
    "task_update_step", "task_decompose", "desire_add", "life_purpose_set",
    "track_parameter_set",
    # note_open/note_close は P3c① で退役済み (旧ログ互換のためここに残す。
    # 後継は memory_open/memory_close)
    "note_open", "note_close",
    "thread_switch", "meta_judgment_finalize", "forget_recalled",
    "record_wait", "resolve_uri", "addon_spell_help",
    "call_playbook", "run_playbook", "list_available_playbooks",
    "get_building_messages", "get_history", "get_memory_weave_context",
    "get_since_last_user_conversation", "get_situation_snapshot",
    "get_system_prompt", "get_task_summary", "get_visual_context",
    "messagelog_get_around", "memopedia_get_tree", "memopedia_health",
    "memopedia_list_fragments", "memopedia_open_page", "memopedia_close_page",
})

_ARG_FALLBACK_KEYS = ["query", "title", "name", "prompt", "url", "keyword", "text"]
_ARG_MAX_LEN = 40
_MAX_PHRASES_PER_LINE = 2


def _truncate(value: str, limit: int = _ARG_MAX_LEN) -> str:
    value = value.strip().replace("\n", " ")
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _extract_arg(args: Dict[str, Any], preferred_keys: Sequence[str]) -> Optional[str]:
    """tool_calls の arguments dict から表示用の代表引数を 1 つ取り出す。"""
    for key in list(preferred_keys) + _ARG_FALLBACK_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate(value)
    return None


def format_tool_phrase(tool_name: str, args: Optional[Dict[str, Any]]) -> Optional[str]:
    """ツール実行 1 件を生活の言葉 1 フレーズに変換する。

    Returns:
        表示フレーズ。表示対象外 (内部ブックキーピング系) なら None。
    """
    if not tool_name or tool_name in _HIDDEN_TOOLS:
        return None
    spec = _TOOL_PHRASES.get(tool_name)
    args = args if isinstance(args, dict) else {}
    if spec is None:
        # 未知のツール (アドオン / MCP 等)。名前をそのまま見せる。
        return f"「{tool_name}」を使った"
    with_arg, without_arg, preferred_keys = spec
    arg = _extract_arg(args, preferred_keys) if "{arg}" in with_arg else None
    if arg is not None:
        return with_arg.format(arg=arg)
    return without_arg


def parse_tool_calls_json(tool_calls_json: Optional[str]) -> List[Tuple[str, Dict[str, Any]]]:
    """pulse_logs.tool_calls (OpenAI 形式 JSON) を (tool_name, args) のリストに展開する。"""
    if not tool_calls_json:
        return []
    try:
        calls = json.loads(tool_calls_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(calls, list):
        return []
    result: List[Tuple[str, Dict[str, Any]]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        raw_args = fn.get("arguments")
        args: Dict[str, Any] = {}
        if isinstance(raw_args, str) and raw_args:
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    args = parsed
            except (TypeError, ValueError):
                pass
        elif isinstance(raw_args, dict):
            args = raw_args
        result.append((name, args))
    return result


# ダイジェストの playbook 表示から除外する名前 (自明・内部的なもの)。
_SKIP_PLAYBOOK_NAMES = frozenset({
    "track_autonomous", "track_user_conversation", "track_social", "track_external",
    "meta_judgment", "meta_judgment_alert", "meta_judgment_idle_empty",
    "meta_judgment_idle_pending", "meta_judgment_running",
    "meta_judgment_life_purpose",
    "meta_exec_speak", "sub_speak", "sub_think_meta",
    "spell_args_decider", "meta_simple_speak",
})


def _format_meta_judgment_result(
    spells_emitted: Optional[Sequence[Dict[str, Any]]],
    track_resolver: Optional[Dict[str, Any]] = None,
) -> str:
    """メタ判断 (MetaLayer: alert/running/idle_pending/idle_empty/life_purpose)
    Pulse の結果を「○○をすることにした」に詳細化する。

    spells_emitted は meta_judgment_finalize が実行した /spell の一覧
    ([{"name":..., "args":...}, ...])。track_resolver は {track_id: ActionTrack}
    の辞書で、Track のタイトルと種別を引くのに使う。
    """
    if not spells_emitted:
        return "次にすることを考えた → 現状を続けることにした"
    for spell in spells_emitted:
        name = spell.get("name", "")
        args = spell.get("args", {})
        if name == "life_purpose_set":
            return "生きる目的を考えた"
        if name == "track_activate":
            tid = args.get("track_id", "")
            track = (track_resolver or {}).get(tid)
            if track is not None:
                ttype = getattr(track, "track_type", "")
                ttitle = getattr(track, "title", "")
                if ttype == "user_conversation":
                    return "次にすることを考えた → あなたと話すことにした"
                if ttype == "social":
                    return "次にすることを考えた → 他のペルソナと話すことにした"
                if ttitle:
                    return f"次にすることを考えた → 「{_truncate(ttitle, 24)}」をすることにした"
            return "次にすることを考えた → 別の作業に切り替えた"
        if name == "track_create":
            ttitle = args.get("title", "")
            # 候補からの昇格 (from_candidate) は「温めてきたやりたいこと」が動き出した瞬間。
            if args.get("from_candidate"):
                if ttitle:
                    return f"やりたかった「{_truncate(ttitle, 24)}」をついに始めることにした"
                return "温めていたやりたいことを始めることにした"
            if ttitle:
                return f"次にすることを考えた → 「{_truncate(ttitle, 24)}」を始めることにした"
            return "次にすることを考えた → 新しいことを始めることにした"
        if name == "track_pause":
            return "次にすることを考えた → 作業を一旦中断した"
        if name == "track_complete":
            return "次にすることを考えた → 作業を完了した"
        if name == "track_abort":
            return "次にすることを考えた → 作業を取りやめた"
    return "次にすることを考えた"


# ----------------------------------------------------------------------
# 判断点 (saiverse/judgment_points.py) の判断結果 → 生活の節目の言葉
# ----------------------------------------------------------------------

# 判断点は 1 日の生活のリズムの節目 (起床 / 会話の後 / 作業の区切り /
# 出来事への反応 / 就寝) ごとに構造化出力で決める判断で、MetaLayer
# (alert/running/idle_* → _format_meta_judgment_result) とは別の判断系列
# (v0.5 「判断点」、judgment_points.md)。kind は
# builtin_data/tools/judgment_finalize.py が messages.metadata.judgment.kind
# に埋め込む値と対応させる (saiverse/judgment_points.py の KIND_* 定数)。
_JUDGMENT_KIND_LABELS: Dict[str, str] = {
    "day_open": "今日一日をどう過ごすか考えた",
    "post_conversation": "話し終えて、次に活かせることを考えた",
    "post_session": "作業を一区切りして、続きをどうするか決めた",
    "on_event": "起きたことにどう向き合うか決めた",
    "day_close": "今日一日をふりかえった",
}


def _format_judgment_point_result(
    judgment_kind: str,
    spells_emitted: Optional[Sequence[Dict[str, Any]]],
    track_resolver: Optional[Dict[str, Any]] = None,
) -> str:
    """判断点 Pulse の結果を「その節目で何を考えたか」+ (分かれば) 実際に
    動いたことの一言に詳細化する。

    判断点の適用結果の大半 (時間割の編成、タスクの裁定、出来事への反応など)
    は /spell を伴わない直接の DB 書き込みで、ここから rich な詳細は取れない
    (builtin_data/tools/judgment_finalize.py 参照)。それでも「どの節目の
    判断だったか」を出すだけで、旧来の一律「次にすることを考えた」よりも
    ずっと具体的になる。/spell が伴った場合 (欲求の書き留め・Track の起動/
    完了など) はその一言を付け足す。
    """
    base = _JUDGMENT_KIND_LABELS.get(judgment_kind, "考えごとをしていた")
    detail: Optional[str] = None
    for spell in spells_emitted or []:
        name = spell.get("name", "")
        args = spell.get("args", {})
        if name == "purpose_seed":
            title = args.get("title", "")
            detail = (
                f"「{_truncate(title, 24)}」をやりたいことに書き留めた"
                if title else "やりたいことを書き留めた"
            )
            break
        if name == "track_create":
            title = args.get("title", "")
            if args.get("from_candidate"):
                detail = (
                    f"やりたかった「{_truncate(title, 24)}」を始めることにした"
                    if title else "温めていたやりたいことを始めることにした"
                )
            else:
                detail = (
                    f"「{_truncate(title, 24)}」を始めることにした"
                    if title else "新しいことを始めることにした"
                )
            break
        if name == "track_complete":
            track = (track_resolver or {}).get(args.get("track_id", ""))
            ttitle = getattr(track, "title", "") if track is not None else ""
            detail = (
                f"「{_truncate(ttitle, 24)}」を完了にした"
                if ttitle else "作業を完了にした"
            )
            break
    if detail:
        return f"{base} → {detail}"
    return base


def build_digest_label(
    track_title: Optional[str],
    track_type: Optional[str],
    line_roles: Sequence[str],
    tool_calls: Sequence[Tuple[str, Dict[str, Any]]],
    *,
    playbook_names: Optional[Sequence[str]] = None,
    playbook_display_names: Optional[Dict[str, str]] = None,
    spells_emitted: Optional[Sequence[Dict[str, Any]]] = None,
    track_resolver: Optional[Dict[str, Any]] = None,
    judgment_kind: Optional[str] = None,
) -> str:
    """Pulse 1 件をダイジェスト 1 行 (生活の言葉) に変換する。

    playbook_display_names は {playbook_name: display_name} の辞書で、
    DB の playbooks.display_name から引いた値を渡す。

    judgment_kind は判断点 (saiverse/judgment_points.py) の判断種別
    ("day_open"/"post_conversation"/"post_session"/"on_event"/"day_close")。
    messages.metadata.judgment.kind から解決した値で、MetaLayer 側の
    meta_judgment (alert/running/idle_*/life_purpose) にはこのタグが無いため
    None のまま渡ってくる。

    優先順:
    1. 判断点 Pulse (judgment_kind あり) → 節目の言葉 + 分かれば実施内容
    2. メタ判断 Pulse (MetaLayer) → spells_emitted から遷移結果を読む
    3. 表示対象ツールの実行があればそのフレーズ (最大 _MAX_PHRASES_PER_LINE 件連結)
    4. 実行された playbook 名から行動種別を列挙
    5. Track タイトルベースの汎用文
    """
    # 1. 判断点 Pulse (day_open/post_conversation/post_session/on_event/day_close)
    if judgment_kind:
        return _format_judgment_point_result(judgment_kind, spells_emitted, track_resolver)

    # 2. メタ判断 Pulse (MetaLayer: alert/running/idle_pending/idle_empty/life_purpose)
    if "meta_judgment" in line_roles:
        return _format_meta_judgment_result(spells_emitted, track_resolver)

    # 3. ツール実行
    phrases: List[str] = []
    for name, args in tool_calls:
        phrase = format_tool_phrase(name, args)
        if phrase and phrase not in phrases:
            phrases.append(phrase)
        if len(phrases) >= _MAX_PHRASES_PER_LINE:
            break
    if phrases:
        return "、".join(phrases)

    # 4. Playbook 名から行動種別 (display_name は呼び出し元が DB から解決して渡す)
    if playbook_names:
        pb_labels: List[str] = []
        seen: set = set()
        for pb in playbook_names:
            if not pb or pb in _SKIP_PLAYBOOK_NAMES:
                continue
            label = playbook_display_names.get(pb, pb.replace("_", " ")) if playbook_display_names else pb.replace("_", " ")
            if label and label not in seen:
                pb_labels.append(label)
                seen.add(label)
            if len(pb_labels) >= _MAX_PHRASES_PER_LINE:
                break
        if pb_labels:
            return " / ".join(pb_labels) + " を行った"

    # 5. 汎用
    title = (track_title or "").strip()
    if title:
        return f"「{_truncate(title, 30)}」を進めた"
    if track_type == "autonomous":
        return "自分の作業を進めた"
    return "考えごとをしていた"


def build_activity_label(
    track_title: Optional[str], track_intent: Optional[str]
) -> Optional[str]:
    """常在インジケータ用の短い活動ラベル (running 自律 Track から)。「何をして
    いるか」を出すもので、「話しかけてよいか」(ライフ由来の活動中/休憩中) とは別。

    題名も意図も空なら **None** を返す (2026-07-14)。かつては "活動中" を返して
    いたが、その文言はライフ由来の「活動中」チップと丸かぶりし、滞在ペルソナ欄で
    緑ドット＋「活動中」が 2 つ並ぶ実機バグの原因になっていた。中身を言えない
    ときは名乗らず黙る — 呼び出し側は None なら何も表示しない。
    """
    base = (track_title or "").strip() or (track_intent or "").strip()
    if not base:
        return None
    return _truncate(base, 12)
