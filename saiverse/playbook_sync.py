"""起動時プレイブック自動同期モジュール。

ファイルベースのプレイブック（builtin_data / expansion_data / user_data）を
スキャンして DB に差分同期する。

優先順位（高い方が勝つ）:
    user_data/<project>/playbooks/public/  >  expansion_data/<addon>/playbooks/public/  >  builtin_data/playbooks/public/

同名プレイブックが複数ソースに存在する場合は最も優先度が高いファイルを採用する。

ハッシュ比較:
    ファイルの JSON を sort_keys=True でシリアライズした SHA-256 (16 文字) を
    DB の source_hash と照合し、差分がある場合のみ更新する。

save_playbook ツール経由で DB を直接編集した場合は source_file / source_hash が
クリアされるため、次回起動時にファイル版で上書きされない（設計上のユーザー優先）。

Orphan の自動削除:
    sync 終了時に scope='public' AND source_file IS NOT NULL でかつ
    source_file が指すファイルが disk に無いレコードを削除する
    (PlaybookPermission の対応行も削除)。
    save_playbook ツール経由で作った Playbook は source_file IS NULL なので
    保護される。addon を一時的に外した場合は対応するファイルが消えて該当
    Playbook も削除されるが、addon を再追加すれば再 import される。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _canonical_hash(data: Any) -> str:
    """JSON の正規化ダンプに対する SHA-256 の先頭 16 文字を返す。"""
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _rel_path(path: Path) -> str:
    """プロジェクトルートからの相対パスを文字列で返す。"""
    try:
        return str(path.resolve().relative_to(_PROJECT_ROOT))
    except ValueError:
        return str(path)


def _collect_file_playbooks(
    collect_errors: Optional[List[Tuple[Path, str]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """ファイルベースのプレイブックを優先順に収集する。

    戻り値: {playbook_name: {"path": Path, "data": dict, "source_rel": str, "hash": str}}
    同名は最初に見つかったもの（= 高優先）のみ保持。

    Args:
        collect_errors: 渡されたリストに、読み込みに失敗したファイルの
            ``(パス, 理由)`` を append する。省略時 (None) は従来どおり
            WARNING ログを出してスキップするだけ (毎起動の sync は壊れた
            ファイル 1 個で起動不能にならないよう寛容のまま)。全置き換え
            (upgrade_handlers.replace_all_playbooks) のような破壊操作の
            呼び出し元だけがこれを使って厳格に検査する。
    """
    from saiverse.data_paths import iter_project_subdirs, PLAYBOOKS_DIR

    result: Dict[str, Dict[str, Any]] = {}

    for playbooks_dir in iter_project_subdirs(PLAYBOOKS_DIR):
        # public/ サブディレクトリを対象にする（personal/building は対象外）
        public_dir = playbooks_dir / "public"
        if not public_dir.exists():
            continue
        for json_path in sorted(public_dir.glob("*.json")):
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception as exc:
                LOGGER.warning("playbook_sync: failed to read %s: %s", json_path, exc)
                if collect_errors is not None:
                    collect_errors.append((json_path, str(exc)))
                continue

            name = data.get("name")
            if not name:
                LOGGER.warning("playbook_sync: missing 'name' in %s, skipping", json_path)
                if collect_errors is not None:
                    collect_errors.append((json_path, "missing 'name' field"))
                continue

            if name in result:
                # 低優先ソースは無視（高優先が既に登録済み）
                continue

            result[name] = {
                "path": json_path,
                "data": data,
                "source_rel": _rel_path(json_path),
                "hash": _canonical_hash(data),
            }

    return result


def _build_db_record(
    name: str,
    data: dict,
    source_rel: str,
    source_hash: str,
) -> dict:
    """Playbook DB レコードのフィールド辞書を構築する。

    description / display_name は JSON では文字列か言語辞書 ({ja, en}) のどちらも
    許される (PlaybookSchema) が、DB 列は文字列なので、日本語を本体の列・英語を
    ``_en`` 列へ分けて書く (import_all_playbooks.py と同じ約束)。
    """
    from saiverse.i18n_utils import split_i18n_columns

    description, description_en = split_i18n_columns(
        data.get("description"), alt_en=data.get("description_en"),
    )
    display_name, display_name_en = split_i18n_columns(
        data.get("display_name"), alt_en=data.get("display_name_en"),
    )
    schema_payload = {
        "name": name,
        "description": description or "",
        "input_schema": data.get("input_schema", []),
        "start_node": data.get("start_node"),
    }
    required_creds = data.get("required_credentials")
    return {
        "description": description or "",
        "description_en": description_en,
        "display_name": display_name,
        "display_name_en": display_name_en,
        "schema_json": json.dumps(schema_payload, ensure_ascii=False),
        "nodes_json": json.dumps(data, ensure_ascii=False),
        "router_callable": bool(data.get("router_callable", False)),
        "user_selectable": bool(data.get("user_selectable", False)),
        "dev_only": bool(data.get("dev_only", False)),
        "required_credentials": (
            json.dumps(required_creds, ensure_ascii=False) if required_creds else None
        ),
        "source_file": source_rel,
        "source_hash": source_hash,
    }


def _prune_orphan_playbooks(db) -> int:
    """source_file が指すファイルが disk に無い public Playbook を削除する。

    対象:
        scope='public' AND source_file IS NOT NULL AND ファイルが disk に無い

    保護:
        - save_playbook ツール経由で作った Playbook (source_file IS NULL)
        - personal/building scope の Playbook

    PlaybookPermission の対応行も削除する。

    Args:
        db: 呼び出し側で管理する DB セッション (commit はここでは行わない)

    Returns:
        削除件数
    """
    from database.models import Playbook, PlaybookPermission

    pruned_count = 0
    candidates = (
        db.query(Playbook)
        .filter(Playbook.scope == "public")
        .filter(Playbook.source_file.isnot(None))
        .all()
    )

    for pb in candidates:
        src_path = Path(pb.source_file)
        if not src_path.is_absolute():
            src_path = _PROJECT_ROOT / src_path

        if src_path.exists():
            continue

        perms_deleted = (
            db.query(PlaybookPermission)
            .filter(PlaybookPermission.playbook_name == pb.name)
            .delete(synchronize_session=False)
        )
        db.delete(pb)
        LOGGER.info(
            "playbook_sync: pruned orphan '%s' (missing source: %s, permissions removed: %d)",
            pb.name, pb.source_file, perms_deleted,
        )
        pruned_count += 1

    return pruned_count


def sync_playbooks_from_files(session_factory=None) -> Dict[str, int]:
    """ファイルベースのプレイブックを DB に差分同期する。

    Args:
        session_factory: DB セッションファクトリ（省略時は database.session.SessionLocal を使用）

    Returns:
        {"imported": int, "updated": int, "skipped": int, "errors": int, "pruned": int}
    """
    from database.models import Playbook

    if session_factory is None:
        from database.session import SessionLocal
        session_factory = SessionLocal

    counts = {"imported": 0, "updated": 0, "skipped": 0, "errors": 0, "pruned": 0}

    try:
        file_playbooks = _collect_file_playbooks()
    except Exception:
        LOGGER.exception("playbook_sync: failed to collect file playbooks")
        return counts

    LOGGER.debug("playbook_sync: found %d file-based playbook(s)", len(file_playbooks))

    db = session_factory()
    try:
        for name, info in file_playbooks.items():
            try:
                existing: Optional[Playbook] = (
                    db.query(Playbook).filter(Playbook.name == name).first()
                )
                fields = _build_db_record(
                    name=name,
                    data=info["data"],
                    source_rel=info["source_rel"],
                    source_hash=info["hash"],
                )

                if existing is None:
                    # 新規インポート
                    record = Playbook(
                        name=name,
                        scope="public",
                        created_by_persona_id=None,
                        building_id=None,
                        **fields,
                    )
                    db.add(record)
                    counts["imported"] += 1
                    LOGGER.info(
                        "playbook_sync: imported '%s' from %s", name, info["source_rel"]
                    )
                elif existing.source_hash != info["hash"]:
                    # ハッシュが変わっている → 更新
                    for k, v in fields.items():
                        setattr(existing, k, v)
                    counts["updated"] += 1
                    LOGGER.info(
                        "playbook_sync: updated '%s' from %s", name, info["source_rel"]
                    )
                elif existing.source_file != info["source_rel"]:
                    # 内容は同一だが提供元の層が移動している (例: builtin → addon
                    # へ切り出し)。source_file を旧パスのまま残すと、直後の
                    # orphan prune が「提供元あり」の Playbook を孤児と誤認して
                    # 削除し、PlaybookPermission も道連れになる (こちらは再
                    # import では戻らない)。内容は触らず提供元だけ付け替える。
                    existing.source_file = info["source_rel"]
                    counts["updated"] += 1
                    LOGGER.info(
                        "playbook_sync: repointed source_file of '%s' -> %s",
                        name, info["source_rel"],
                    )
                else:
                    # 差分なし
                    counts["skipped"] += 1
                    LOGGER.debug("playbook_sync: '%s' is up-to-date, skipping", name)

            except Exception:
                LOGGER.exception("playbook_sync: error processing playbook '%s'", name)
                counts["errors"] += 1

        # Disk から消えた public プレイブックを DB からも削除
        try:
            counts["pruned"] = _prune_orphan_playbooks(db)
        except Exception:
            LOGGER.exception("playbook_sync: error during orphan prune")
            counts["errors"] += 1

        db.commit()

    except Exception:
        LOGGER.exception("playbook_sync: DB error during sync")
        db.rollback()
    finally:
        db.close()

    LOGGER.info(
        "playbook_sync: done — imported=%d updated=%d skipped=%d pruned=%d errors=%d",
        counts["imported"], counts["updated"], counts["skipped"], counts["pruned"], counts["errors"],
    )
    return counts


# ---------------------------------------------------------------------------
# 利用者の手で直す必要がある Playbook の警告 (自動では書き換えない)
# ---------------------------------------------------------------------------

#: 本文に書かれた旧形式のシステム通知のタグ (``<system>`` / ``</system>`` /
#: ``<system pass=...>``)。``<system_prompt>`` のような別の語には当てない。
_LEGACY_SYSTEM_TAG_RE = re.compile(r"</?system(?:\s|>)", re.IGNORECASE)
#: テンプレートの差し込み (``{name}`` / ``{name.key}``) の先頭の名前。
_TEMPLATE_ROOT_RE = re.compile(r"\{([A-Za-z_]\w*)")


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_strings(v)


def _tool_result_keys(nodes: List[Any]) -> set:
    """TOOL / TOOL_CALL ノードの結果が入る state の名前 (``last`` を含む)。"""
    keys = {"last"}
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") not in ("tool", "tool_call"):
            continue
        for key in (node.get("output_key"), node.get("id")):
            if isinstance(key, str) and key:
                keys.add(key)
        for key in node.get("output_keys") or []:
            if isinstance(key, str) and key:
                keys.add(key)
    return keys


def _creates_legacy_system_notice(nodes: List[Any]) -> bool:
    """旧形式のシステム通知を作る Playbook か。

    docs/intent/system_notice_delivery.md: システム通知は role=system・タグ無しの
    本文で記録する。旧形式は (1) 本文に ``<system>`` タグを書く、(2) MEMORIZE が
    ツールの結果を role=user のまま保存する (タグ無しの機構の文がユーザーの発言
    として残る)、のどちらか。
    """
    if any(_LEGACY_SYSTEM_TAG_RE.search(s) for s in _iter_strings(nodes)):
        return True
    tool_keys = _tool_result_keys(nodes)
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") != "memorize":
            continue
        if str(node.get("role") or "").lower() != "user":
            continue
        # 実行側 (sea/runtime_engine.py の lg_memorize_node、sea/langgraph_runner.py) と
        # 同じ既定: action の省略・空文字は ``{last}`` (直前のノードの結果) を記録する。
        action = node.get("action") or "{last}"
        if not isinstance(action, str):
            continue
        if tool_keys & set(_TEMPLATE_ROOT_RE.findall(action)):
            return True
    return False


def playbook_compat_alerts(session_factory=None) -> List[Dict[str, Any]]:
    """有効な Playbook (DB の行) のうち、利用者の手で更新が要るものを startup_alerts の形で返す。

    - 旧形式のシステム通知を作る Playbook (:func:`_creates_legacy_system_notice`)。
    - TOOL ノードが、いま登録されていないツールを参照している Playbook (実行時と
      同じ ``canonicalize_tool_name`` で引く。削除したツールに限らない一般の検査)。
      設定にある MCP サーバーの名前で始まる参照は、サーバーが未接続なだけの
      可能性があるので別の警告 (``playbook_missing_mcp_tool``) にする。ペルソナ
      ごとに接続するサーバー (``scope: per_persona``) のツールは起動時に無いのが
      正常なので、どちらの警告にも出さない。

    どちらも**自動では書き換えない** — 利用者が世界編集や上位層 (user_data /
    expansion_data) で作った・変えた Playbook を勝手に変えないため。画面に出して
    編集画面での更新を促すだけ。検査は起動ごと。ツールの一覧が出そろってから
    (MCP の接続の後で) 呼ぶ。
    """
    from database.models import Playbook
    from tools import TOOL_REGISTRY, canonicalize_tool_name

    if session_factory is None:
        from database.session import SessionLocal
        session_factory = SessionLocal

    db = session_factory()
    try:
        rows = db.query(Playbook.name, Playbook.nodes_json).all()
    finally:
        db.close()

    legacy: List[str] = []
    missing: List[Tuple[str, str]] = []
    for name, raw in rows:
        try:
            data = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            continue
        nodes = data.get("nodes") if isinstance(data, dict) else None
        if not isinstance(nodes, list):
            continue
        if _creates_legacy_system_notice(nodes):
            legacy.append(name)
        for node in nodes:
            if not isinstance(node, dict) or node.get("type") != "tool":
                continue
            tool = node.get("action")
            if isinstance(tool, str) and tool and canonicalize_tool_name(tool) not in TOOL_REGISTRY:
                missing.append((name, tool))

    # MCP のツールは ``<サーバー名>__<ツール名>`` で登録される (tools/mcp_client.py)。
    # 設定にあるサーバーの名前で始まる参照は、サーバーが起動時に繋がらなかった
    # だけかもしれないので、本当に存在しないツールとは分けて知らせる。素の名前で
    # 参照している場合は、未接続の MCP のツールかどうかを見分けられない。
    # ペルソナごとに接続するサーバー (scope が per_persona) は、起動時に繋がず
    # 各ペルソナの Pulse の頭でツールを登録するのが設計どおりなので、起動時に
    # 見つからないのは正常。警告に出すと毎起動必ず出て正常な構成を異常に
    # 見せるため、どちらの警告にも出さない。scope が instance_template の
    # サーバーも起動時にツールを登録しない (conn.call_tool で直接呼ぶ —
    # tools/mcp_client.py) ので同じ扱い。警告するのは global のサーバーだけ。
    missing_mcp: List[Tuple[str, str]] = []
    if missing:
        scopes = _mcp_server_scopes()
        remaining: List[Tuple[str, str]] = []
        for name, tool in missing:
            scope = _mcp_scope_of_tool(tool, scopes)
            if scope is None:
                remaining.append((name, tool))
            elif scope not in ("per_persona", "instance_template"):
                missing_mcp.append((name, tool))
        missing = remaining

    alerts: List[Dict[str, Any]] = []
    if legacy:
        LOGGER.warning("playbook_sync: playbooks that create legacy system notices: %s", legacy)
        alerts.append({
            "id": "playbook_legacy_system_notice",
            "level": "warning",
            "title": "旧形式のシステム通知を作る Playbook があります",
            "message": (
                "次の Playbook は、システム通知を古い形 (本文を system のタグで囲む形、または"
                "ツールの結果をユーザーの発言として記録する形) で作ります。自動では"
                "書き換えていません。Playbook の編集画面で更新してください: "
                + ", ".join(sorted(legacy))
            ),
            "details": {"playbooks": sorted(legacy)},
        })
    if missing:
        LOGGER.warning("playbook_sync: playbooks referencing unregistered tools: %s", missing)
        alerts.append({
            "id": "playbook_missing_tool",
            "level": "warning",
            "title": "存在しないツールを参照している Playbook があります",
            "message": (
                "次の Playbook は、いま登録されていないツールを呼びます (そのノードは実行時に"
                "失敗します)。Playbook の編集画面で更新してください: "
                + ", ".join(f"{name} → {tool}" for name, tool in sorted(missing))
            ),
            "details": {"references": [{"playbook": n, "tool": t} for n, t in sorted(missing)]},
        })
    if missing_mcp:
        LOGGER.warning(
            "playbook_sync: playbooks referencing tools of MCP servers that are not connected: %s",
            missing_mcp,
        )
        alerts.append({
            "id": "playbook_missing_mcp_tool",
            "level": "warning",
            "title": "MCP サーバーのツールが見つからない Playbook があります",
            "message": (
                "次の Playbook は、いま登録されていない MCP サーバーのツールを呼びます。"
                "MCP サーバーが未接続なだけの可能性があります。サーバーを接続できる状態に"
                "して起動し直しても、まだこの警告が出る場合だけ、Playbook の編集画面で"
                "更新してください: "
                + ", ".join(f"{name} → {tool}" for name, tool in sorted(missing_mcp))
            ),
            "details": {
                "references": [{"playbook": n, "tool": t} for n, t in sorted(missing_mcp)],
            },
        })
    return alerts


def _mcp_server_scopes() -> Dict[str, str]:
    """設定にある MCP サーバーの名前と、その接続の種別 (``scope``)。

    サーバー名は tools/mcp_config.load_mcp_configs のキー (アドオンのサーバーは
    ``<アドオン名>__`` 付き) で、ツールはその後ろに ``__<ツール名>`` を付けて
    登録される。種別は tools/mcp_client.MCPClientManager の起動処理と同じく
    ``scope`` を小文字にして読み、無ければ ``global``。設定を読めなければ空
    (どの参照も MCP のものとみなさない)。
    """
    try:
        from tools.mcp_config import load_mcp_configs

        return {
            server: str((cfg or {}).get("scope", "global")).lower()
            for server, cfg in load_mcp_configs().items()
        }
    except Exception:
        LOGGER.warning("playbook_sync: could not read MCP server configs", exc_info=True)
        return {}


def _mcp_scope_of_tool(tool: str, scopes: Dict[str, str]) -> Optional[str]:
    """ツール名 ``<サーバー名>__<ツール名>`` が属するサーバーの種別。属さなければ None。

    名前が前方で重なるサーバー (``foo`` と ``foo__bar``) は長い方を採る。
    """
    matches = [server for server in scopes if tool.startswith(f"{server}__")]
    if not matches:
        return None
    return scopes[max(matches, key=len)]
