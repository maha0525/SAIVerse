"""Muse (Meta personal AI) 公式エクスポートの取り込み API。

ChatGPT / Claude 公式取り込みと同じ「preview → 選択 → 背景取り込み → status
ポーリング」の形。パースは tools/utilities/muse_importer.py、埋め込みの後追い
生成は import_chatlog の _launch_import_embedding を共用する。
Muse のエクスポートは会話が通常 1 件で、カテゴリ・memories・プロジェクト指示を
持たないため、それらのエンドポイントや分岐は作らない。

後段として、エクスポート ZIP に含まれない SOUL.md / IDENTITY.md / MEMORY.md /
AGENTS.md を個別に受け取り、システムプロンプト追記・コア記憶・Memopedia へ
同期で取り込む (preview で判定結果を持ち、import で書く)。
設計: docs/intent/muse_export_import.md
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File
from api.deps import get_manager
from api.file_safety import safe_upload_suffix, write_upload_bounded
from .models import (
    ConversationSummary,
    MuseFileImportRequest,
    MuseFilePreviewEntry,
    MuseFilePreviewResponse,
    MusePreviewResponse,
    MuseImportRequest,
    OfficialImportStatusResponse,
)
from .import_chatlog import (
    CHATLOG_IMPORT_MAX_BYTES,
    _is_placeholder_title,
    _launch_import_embedding,
    _saved_summary,
    _with_embedding_status,
)
from .utils import ensure_persona_exists, get_adapter
# 出典見出しの生成は CLI と共通にする (不変条件 6)。モジュール先頭で読むのは、
# タスク内での遅延 import だと、テストが差し替えた SAIMemoryAdapter を CLI 側の
# 名前束縛が取り込んでしまうため。
from scripts.import_muse_conversations import build_header_payload
import tempfile
import threading
import logging
import re
import uuid
import zipfile
from pathlib import Path
from typing import List, Optional

LOGGER = logging.getLogger(__name__)
router = APIRouter()

# preview → import の間、解析結果を持つ (import_claude の _claude_export_cache と同形)
_muse_export_cache: dict = {}

# 後段: SOUL / IDENTITY / MEMORY / AGENTS の個別ファイル取り込み。
# cache_key -> {"persona_id": str, "files": {kind: {"filename", "text", "items"}},
#               "applied_steps": [str]  # 再試行時の二重適用を防ぐ適用済み記録}
_muse_file_cache: dict = {}

_MEMOPEDIA_TRUNK_TITLE = "Muse引っ越し"
_MEMOPEDIA_TRUNK_PARENT = "root_terms"
_AGENTS_PAGE_TITLE = "作業ルール(Muse時)"

_FILE_KIND_BY_NAME = {
    "soul.md": "soul",
    "identity.md": "identity",
    "memory.md": "memory",
    "agents.md": "agents",
}
_FILE_DESTINATION = {
    "soul": "system_prompt",
    "identity": "system_prompt",
    "memory": "core_memory",
    "agents": "memopedia",
}

_muse_import_status: dict = {}
_muse_import_lock = threading.Lock()


def _resolve_records(export_file, conversation_ids: list) -> list:
    """selector を identifier として解決し、見つからなければ数値インデックスにする。

    フロントは選択行の idx を文字列にして送るため、両方を受け付ける。
    同じレコードが複数回選ばれても一度だけ返す。
    """
    records = []
    conversations = list(export_file.conversations)
    for selector in conversation_ids:
        text = str(selector)
        record = export_file.get_by_identifier(text)
        if record is None:
            try:
                idx = int(text)
            except (TypeError, ValueError):
                idx = None
            if idx is not None and 0 <= idx < len(conversations):
                record = conversations[idx]
        if record is not None and record not in records:
            records.append(record)
    return records


def _split_memory_items(text: str) -> list[dict]:
    """MEMORY.md をコア記憶の項目に分割する。

    見出し (ATX) があれば節ごとに 1 項目とし、見出し行は本文に含めたまま
    タイトルにする。最初の見出しより前の前文も 1 項目 (title=None)。
    見出しが無い、または節が 1 つだけなら、空行区切りの段落へフォールバックし、
    それでも 1 項目しか得られなければ空でない行を 1 行ずつ項目にする。
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    heading_re = re.compile(r"^#{1,6}\s+.*", re.MULTILINE)
    headings = list(heading_re.finditer(normalized))

    sections: list[dict] = []
    if headings:
        preamble = normalized[:headings[0].start()]
        if preamble.strip():
            sections.append({"title": None, "content": preamble.strip()})
        for idx, match in enumerate(headings):
            end = headings[idx + 1].start() if idx + 1 < len(headings) else len(normalized)
            content = normalized[match.start():end].strip()
            if content:
                sections.append({
                    "title": match.group(0).lstrip("#").strip(),
                    "content": content,
                })

    if len(sections) >= 2:
        return sections

    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n{2,}", normalized)
        if paragraph.strip()
    ]
    if len(paragraphs) > 1:
        return [{"title": None, "content": paragraph} for paragraph in paragraphs]

    return [
        {"title": None, "content": line.strip()}
        for line in normalized.split("\n")
        if line.strip()
    ]


def _detect_file_kind(filename: str | None) -> Optional[str]:
    """ファイル名 (ベース名、大小文字は不問) から取り込み種別を判定する。"""
    return _FILE_KIND_BY_NAME.get(Path(filename or "").name.lower())


def _add_core_memory_items(
    conn, contents: list[str]
) -> tuple[list[tuple[int, str]], int]:
    """MEMORY.md の分割項目をコア記憶として追加する。

    生存中のコア記憶に同じ本文があればスキップする (再取り込みで二重にならない)。
    ((core_id, 本文) の一覧, スキップ数) を返す。呼び出し側が adapter の錠前を
    持つこと。
    """
    from sai_memory.core_memory import add_core_memory, list_core_memories

    existing = {m.content for m in list_core_memories(conn)}
    added: list[tuple[int, str]] = []
    skipped = 0
    for content in contents:
        if content in existing:
            skipped += 1
            continue
        core_id = add_core_memory(conn, content, confirmed=1)
        existing.add(content)
        added.append((core_id, content))
    return added, skipped


def _notify_muse_core_memory_import(adapter, added: list[tuple[int, str]]) -> None:
    """コア記憶を追加した事実を知覚バッファ経由でペルソナへ知らせる。"""
    from .core_memory import _notify_persona_correction

    lines = [
        f"ユーザーが Muse からの引っ越しで、あなたのコア記憶に {len(added)} 件を追加しました。",
        "追加された内容:",
    ]
    for core_id, content in added:
        lines.append(f"- core:{core_id}: {content}")
    _notify_persona_correction(adapter, "\n".join(lines))


def _upsert_agents_page(memopedia, text: str) -> str:
    """AGENTS.md を「Muse引っ越し」幹の下のページとして create-or-update する。

    再取り込みで本文が積み上がらないよう、同名ページがあれば本文と要約を
    差し替える。返り値は "created" または "updated"。
    """
    trunk = next(
        (t for t in memopedia.get_trunks() if t.title == _MEMOPEDIA_TRUNK_TITLE),
        None,
    )
    if trunk is None:
        trunk = memopedia.create_page(
            parent_id=_MEMOPEDIA_TRUNK_PARENT,
            title=_MEMOPEDIA_TRUNK_TITLE,
            summary="Muse (Meta personal AI) からの転記",
            is_trunk=True,
            edit_source="muse_import",
        )
    summary = "Muse (Meta personal AI) 利用時の作業ルール (AGENTS.md から転記)"
    trunk_full = memopedia.get_page_full(trunk.id) or {}
    child = next(
        (c for c in trunk_full.get("children", []) if c["title"] == _AGENTS_PAGE_TITLE),
        None,
    )
    if child is not None:
        memopedia.update_page(
            child["id"],
            summary=summary,
            content=text,
            edit_source="muse_import",
        )
        return "updated"
    memopedia.create_page(
        parent_id=trunk.id,
        title=_AGENTS_PAGE_TITLE,
        summary=summary,
        content=text,
        edit_source="muse_import",
    )
    return "created"


def _run_muse_import_task(
    persona_id: str,
    cache_key: str,
    conversation_ids: list,
    skip_embedding: bool,
):
    """選択された Muse 会話を背景で SAIMemory に取り込む。"""
    with _muse_import_lock:
        _muse_import_status[persona_id] = {
            "running": True, "progress": 0, "total": 0, "message": "Starting import..."
        }

    try:
        cached = _muse_export_cache.get(cache_key)
        if not cached:
            with _muse_import_lock:
                _muse_import_status[persona_id] = {
                    "running": False, "message": "Preview expired. Please upload again.",
                    "success": False,
                }
            return

        export_file = cached["export"]
        selected_records = _resolve_records(export_file, conversation_ids)
        if not selected_records:
            with _muse_import_lock:
                _muse_import_status[persona_id] = {
                    "running": False, "message": "No valid conversations found.",
                    "success": False,
                }
            return

        from saiverse_memory import SAIMemoryAdapter
        from saiverse_memory.adapter import EMBEDDING_DEFERRED
        adapter = SAIMemoryAdapter(persona_id)
        handed_off = False
        embedding_chunks = 0 if skip_embedding else EMBEDDING_DEFERRED

        try:
            total_conversations = len(selected_records)
            imported_count = 0
            msg_count = 0
            imported_ids: List[str] = []

            for record in selected_records:
                # CLI と同じく、会話の先頭に出典見出し (システム通知) を置く
                # (docs/intent/muse_export_import.md の不変条件 6)。経路によって
                # 記憶の内容が変わらないよう、見出しも埋め込み保留の対象にする。
                payloads = [build_header_payload(record)]
                payloads.extend(record.iter_memory_payloads())
                thread_suffix = record.conversation_id or record.identifier

                for payload in payloads:
                    # Never embed while importing (import_chatlog と同じ):
                    # 空ベクトル行と一緒に保存し、取り込み後に背景で埋め込む。
                    payload["embedding_chunks"] = embedding_chunks
                    mid = adapter.append_persona_message(payload, thread_suffix=thread_suffix)
                    if mid:
                        imported_ids.append(mid)
                    msg_count += 1

                if not _is_placeholder_title(record.title):
                    adapter.set_thread_title(thread_suffix, record.title)

                imported_count += 1
                with _muse_import_lock:
                    _muse_import_status[persona_id] = {
                        "running": True, "progress": imported_count, "total": total_conversations,
                        "message": f"Imported {imported_count}/{total_conversations} conversations ({msg_count} messages)...",
                    }

            embedding = not skip_embedding and bool(imported_ids)
            if embedding:
                handed_off = _launch_import_embedding(persona_id, adapter, len(imported_ids))
            save_failure = _saved_summary(msg_count, len(imported_ids))
            if save_failure:
                LOGGER.warning("Muse import for %s: %s", persona_id, save_failure)
            with _muse_import_lock:
                _muse_import_status[persona_id] = {
                    "running": False, "progress": imported_count, "total": total_conversations,
                    "message": (
                        f"Imported {imported_count} conversations: {save_failure}"
                        if save_failure
                        else f"Imported {imported_count} conversations ({msg_count} messages)."
                    ),
                    "success": save_failure is None,
                    "conversations": imported_count, "messages": len(imported_ids),
                    "embedding": embedding,
                }
        finally:
            if not handed_off:
                adapter.close()

    except Exception as e:
        LOGGER.exception("Muse import failed for %s", persona_id)
        with _muse_import_lock:
            _muse_import_status[persona_id] = {
                "running": False, "message": f"Error: {str(e)}", "success": False,
            }
    finally:
        if cache_key in _muse_export_cache:
            cached = _muse_export_cache.pop(cache_key, None)
            if cached and cached.get("tmp_path"):
                try:
                    cached["tmp_path"].unlink()
                except Exception:
                    LOGGER.warning("Failed to delete temp file", exc_info=True)


@router.post("/{persona_id}/import/muse/preview", response_model=MusePreviewResponse)
async def preview_muse_export(
    persona_id: str,
    file: UploadFile = File(...),
    manager = Depends(get_manager),
):
    """Muse 公式エクスポートの ZIP を解析し、取り込み候補の一覧を返す。"""
    ensure_persona_exists(persona_id, manager)

    suffix = safe_upload_suffix(file.filename)
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = Path(tmp.name)
    tmp_path.unlink()
    await write_upload_bounded(file, tmp_path, CHATLOG_IMPORT_MAX_BYTES)

    def _cleanup():
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                LOGGER.warning("Failed to delete temp file", exc_info=True)

    try:
        from tools.utilities.muse_importer import MuseExport

        try:
            export = MuseExport(tmp_path)
        except (FileNotFoundError, ValueError, zipfile.BadZipFile) as e:
            _cleanup()
            raise HTTPException(status_code=400, detail=f"Could not parse Muse export: {e}")

        summaries: List[ConversationSummary] = []
        for idx, record in enumerate(export.conversations):
            summary_dict = record.to_summary_dict(preview_chars=120)
            summaries.append(ConversationSummary(
                idx=idx,
                id=str(summary_dict.get("id", ""))[:40],
                conversation_id=summary_dict.get("conversation_id"),
                title=str(summary_dict.get("title", ""))[:50],
                create_time=summary_dict.get("create_time"),
                update_time=summary_dict.get("update_time"),
                message_count=summary_dict.get("message_count", 0),
                preview=(summary_dict.get("first_user_preview") or "")[:100],
            ))

        export_summaries = export.summaries(preview_chars=120)
        first_summary = export_summaries[0] if export_summaries else {}
        workspace_file_count = int(first_summary.get("workspace_file_count", 0) or 0)
        exported_at = first_summary.get("exported_at")

        total = len(summaries)
        if total == 0:
            _cleanup()
            return MusePreviewResponse(
                conversations=[], cache_key="", total_count=0,
                workspace_file_count=workspace_file_count, exported_at=exported_at,
            )

        cache_key = str(uuid.uuid4())
        _muse_export_cache[cache_key] = {
            "export": export,
            "tmp_path": tmp_path,
            "persona_id": persona_id,
        }
        return MusePreviewResponse(
            conversations=summaries,
            cache_key=cache_key,
            total_count=total,
            workspace_file_count=workspace_file_count,
            exported_at=exported_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        _cleanup()
        raise HTTPException(status_code=400, detail=f"Preview failed: {str(e)}")


@router.post("/{persona_id}/import/muse")
def import_muse_export(
    persona_id: str,
    request: MuseImportRequest,
    background_tasks: BackgroundTasks,
    manager = Depends(get_manager),
):
    """プレビュー済み Muse エクスポートから選択した会話を背景で取り込む。"""
    ensure_persona_exists(persona_id, manager)

    with _muse_import_lock:
        status = _muse_import_status.get(persona_id, {})
        if status.get("running"):
            raise HTTPException(status_code=409, detail="Import already in progress.")

    cached = _muse_export_cache.get(request.cache_key)
    if not cached:
        raise HTTPException(status_code=400, detail="Preview expired or invalid. Please upload the file again.")
    if cached["persona_id"] != persona_id:
        raise HTTPException(status_code=400, detail="Persona ID mismatch")
    if not request.conversation_ids:
        raise HTTPException(status_code=400, detail="No conversations selected for import.")

    export_file = cached["export"]
    selected_records = _resolve_records(export_file, request.conversation_ids)
    if not selected_records:
        raise HTTPException(status_code=400, detail="No valid conversations found for the given selection.")

    # 背景タスクが状態を立てる前に同ペルソナの要求が重なると、冒頭のチェックを
    # 両方とも通過して同じ会話を二重に取り込んでしまう。検証が済んだこの時点で、
    # ロック内で実行中チェックと状態の予約を一体で行ってからタスクを登録する。
    with _muse_import_lock:
        status = _muse_import_status.get(persona_id, {})
        if status.get("running"):
            raise HTTPException(status_code=409, detail="Import already in progress.")
        _muse_import_status[persona_id] = {
            "running": True, "progress": 0, "total": len(selected_records),
            "message": "Starting import...",
        }

    background_tasks.add_task(
        _run_muse_import_task,
        persona_id,
        request.cache_key,
        list(request.conversation_ids),
        request.skip_embedding,
    )
    return {
        "success": True,
        "message": f"Import started for {len(selected_records)} conversations. Check status endpoint for progress.",
        "status": {"running": True, "progress": 0, "total": len(selected_records), "message": "Starting..."},
    }


@router.get("/{persona_id}/import/muse/status", response_model=OfficialImportStatusResponse)
def get_muse_import_status(persona_id: str, manager = Depends(get_manager)):
    """Muse 取り込みタスクの状態 (背景埋め込みの進捗を含む)。"""
    with _muse_import_lock:
        status = _muse_import_status.get(persona_id, {"running": False, "message": "No import task has been run."})
    return OfficialImportStatusResponse(**_with_embedding_status(persona_id, status))

@router.post("/{persona_id}/import/muse/files/preview", response_model=MuseFilePreviewResponse)
async def preview_muse_files(
    persona_id: str,
    files: List[UploadFile] = File(...),
    manager = Depends(get_manager),
):
    """SOUL / IDENTITY / MEMORY / AGENTS の個別アップロードを判定する。

    ファイル名 (ベース名、大小文字は不問) で種別を決め、MEMORY.md はその場で
    コア記憶の項目へ分割して件数を返す。判定できないファイルは対象外として
    一覧に残すだけ。同じ種別が複数来たら最後のファイルを採用する。
    """
    ensure_persona_exists(persona_id, manager)

    parsed: dict = {}
    entries: List[MuseFilePreviewEntry] = []

    for upload in files:
        filename = upload.filename or ""
        kind = _detect_file_kind(filename)
        suffix = safe_upload_suffix(filename)
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = Path(tmp.name)
        tmp_path.unlink()

        text = ""
        try:
            await write_upload_bounded(upload, tmp_path, CHATLOG_IMPORT_MAX_BYTES)
            try:
                text = tmp_path.read_bytes().decode("utf-8-sig")
            except UnicodeDecodeError:
                # 判定できても中身が読めないファイルは対象外にする。
                kind = None
                text = ""
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                LOGGER.warning("Failed to delete temp file", exc_info=True)

        if kind is None:
            entries.append(MuseFilePreviewEntry(filename=filename))
            continue

        items = _split_memory_items(text) if kind == "memory" else []
        parsed[kind] = {"filename": filename, "text": text, "items": items}
        entries.append(MuseFilePreviewEntry(
            filename=filename,
            kind=kind,
            destination=_FILE_DESTINATION.get(kind),
            item_count=len(items) if kind == "memory" else (1 if text else 0),
            char_count=len(text),
        ))

    # 同じ種別の重複は採用された最後のファイルだけを残す (対象外の行は全部残す)。
    last_index_by_kind = {
        entry.kind: idx for idx, entry in enumerate(entries) if entry.kind is not None
    }
    preview_files = [
        entry
        for idx, entry in enumerate(entries)
        if entry.kind is None or last_index_by_kind.get(entry.kind) == idx
    ]

    if not parsed:
        return MuseFilePreviewResponse(files=preview_files, cache_key="", total_count=0)

    cache_key = str(uuid.uuid4())
    _muse_file_cache[cache_key] = {
        "persona_id": persona_id,
        "files": parsed,
        "applied_steps": [],
    }
    return MuseFilePreviewResponse(
        files=preview_files,
        cache_key=cache_key,
        total_count=len(parsed),
    )


@router.post("/{persona_id}/import/muse/files")
def import_muse_files(
    persona_id: str,
    request: MuseFileImportRequest,
    manager = Depends(get_manager),
):
    """プレビュー済みの個別ファイルをペルソナへ同期で取り込む。

    システムプロンプト追記を最初に行い、失敗したら記憶側は書かない。成功した
    場合だけ preview のキャッシュを破棄し、同じプレビューの二重実行を防ぐ。
    記憶側の失敗でキャッシュが残った場合の再試行では、適用済みステップ
    (applied_steps) をスキップし、プロンプトの二重追記を防ぐ。記憶側の
    書き込み自体は冪等 (コア記憶は重複スキップ、Memopedia は作成/更新)。
    """
    ensure_persona_exists(persona_id, manager)

    cached = _muse_file_cache.get(request.cache_key)
    if not cached:
        raise HTTPException(
            status_code=400,
            detail="Preview expired or invalid. Please upload the files again.",
        )
    if cached["persona_id"] != persona_id:
        raise HTTPException(status_code=400, detail="Persona ID mismatch")

    cached_files = cached["files"]
    requested = request.kinds or list(cached_files.keys())
    kinds: List[str] = []
    for kind in requested:
        if kind in cached_files and kind not in kinds:
            kinds.append(kind)
    if not kinds:
        raise HTTPException(status_code=400, detail="No valid files selected for import.")

    appended_kinds: List[str] = []
    instructions: List[str] = []
    for kind in ("soul", "identity"):
        if kind in kinds:
            text = cached_files[kind]["text"]
            if text.strip():
                instructions.append(text)
                appended_kinds.append(kind)
    if instructions:
        # 失敗 (HTTPException) はそのまま伝播させ、記憶側を始めない。
        # プロンプト追記は即時永続化されるため、記憶側の失敗後に同じ
        # キャッシュで再試行された場合は二重追記にならないよう抑止する。
        from .import_claude import _append_instructions_to_system_prompt
        applied_steps = cached.setdefault("applied_steps", [])
        if "prompt" not in applied_steps:
            _append_instructions_to_system_prompt(manager, persona_id, instructions)
            applied_steps.append("prompt")

    core_added: list[tuple[int, str]] = []
    core_skipped = 0
    core_total_chars = 0
    memopedia_action: Optional[str] = None

    with get_adapter(persona_id, manager) as adapter:
        try:
            if "agents" in kinds and cached_files["agents"]["text"].strip():
                from sai_memory.memopedia import Memopedia
                memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)
                memopedia_action = _upsert_agents_page(
                    memopedia, cached_files["agents"]["text"],
                )

            if "memory" in kinds:
                from sai_memory.core_memory import total_core_memory_chars
                contents = [item["content"] for item in cached_files["memory"]["items"]]
                with adapter._db_lock:
                    core_added, core_skipped = _add_core_memory_items(adapter.conn, contents)
                    core_total_chars = total_core_memory_chars(adapter.conn)
        except HTTPException:
            raise
        except Exception as e:
            LOGGER.exception("Muse file import failed for %s", persona_id)
            raise HTTPException(status_code=500, detail=f"Memory import error: {e}")

        if core_added:
            _notify_muse_core_memory_import(adapter, core_added)

    from .core_memory import _resolve_budget
    core_budget = _resolve_budget(manager, persona_id)

    # 成功したプレビューだけを破棄する (同じ preview の二重実行を防ぐ)。
    _muse_file_cache.pop(request.cache_key, None)

    parts: List[str] = []
    if appended_kinds:
        parts.append(f"appended {len(appended_kinds)} file(s) to the system prompt")
    if "memory" in kinds:
        parts.append(
            f"added {len(core_added)} core memory item(s)"
            + (f" ({core_skipped} already existed)" if core_skipped else "")
        )
    if memopedia_action:
        parts.append(f"Memopedia page {memopedia_action}")
    message = "; ".join(parts) + "." if parts else "No changes were necessary."

    return {
        "success": True,
        "system_prompt_appended": appended_kinds,
        "core_created": len(core_added),
        "core_skipped": core_skipped,
        "core_total_chars": core_total_chars,
        "core_budget": core_budget,
        "memopedia_action": memopedia_action,
        "trunk_title": _MEMOPEDIA_TRUNK_TITLE,
        "message": message,
    }
