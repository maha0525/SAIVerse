"""ペルソナまるごとパック (persona-pack) の API。

正典: docs/intent/persona_pack.md。エンジンは saiverse_memory/persona_pack/
(export_persona_pack / import_persona_pack)。ここはそれを本体サーバーへ配線する:

- ``GET  /{persona_id}/persona-pack``                — 書き出し (読み取り専用)。ZIP を返す。
- ``POST /{persona_id}/persona-pack/import``         — 既存のペルソナへ合流 (mode="merge")。
- ``POST /persona-pack/import-new``                  — 新しいペルソナを作って取り込む (mode="new")。

取り込みは同期で行い、結果の内訳 (PersonaPackImportResult) を JSON で返す。
取り込んだ message は空のベクトル行を持って入る (importer.py の docstring) ので、
import_chatlog.py と同じ ``_launch_import_embedding`` で埋め込みの再生成を裏で起こす。
生きているペルソナの adapter は import_chatlog.py と同じく触らない (取り込みは別の
接続で memory.db に書き、埋め込みは使い捨ての adapter が行う)。プロフィールを上書きした
ときだけ、読み込み済みのペルソナへ保存経路 (manager.update_ai) で値を写す。

合流は Beat ロックの内側で行い、あらすじ生成・補修のジョブが終端していなければ
409 で断る。Chronicle ページを取り込んだ合流では、冷えた窓への presented_raw の印と
次の編纂の予測 (束ねの回数・content_stale の件数) を結果の ``chronicle_after_merge``
に載せる (_chronicle_after_merge)。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from api.deps import get_manager
from api.file_safety import write_upload_bounded
from .utils import ensure_persona_exists

LOGGER = logging.getLogger(__name__)
router = APIRouter()

PERSONA_PACK_IMPORT_MAX_BYTES = 2 * 1024 * 1024 * 1024

# 取り込み中のペルソナ (同じペルソナへの取り込みを同時に二つ走らせない)
_importing: set = set()
_importing_lock = threading.Lock()

# ファイル名に使えない文字 (Windows の禁止文字と制御文字)
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _world_paths(manager: Any) -> tuple[Path, Path]:
    """取り込み先 / 書き出し元の saiverse.db と SAIVERSE_HOME。"""
    return Path(manager.db_path), Path(manager.saiverse_home)


@contextmanager
def _claim_import(persona_id: str) -> Iterator[None]:
    with _importing_lock:
        if persona_id in _importing:
            raise HTTPException(
                status_code=409, detail="A persona pack import is already running for this persona.",
            )
        _importing.add(persona_id)
    try:
        yield
    finally:
        with _importing_lock:
            _importing.discard(persona_id)


async def _save_upload(file: UploadFile) -> Path:
    """アップロードを一時ファイルへ書く (呼び出し側が消す)。"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip", prefix="persona_pack_") as tmp:
        tmp_path = Path(tmp.name)
    tmp_path.unlink()
    await write_upload_bounded(file, tmp_path, PERSONA_PACK_IMPORT_MAX_BYTES)
    return tmp_path


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        LOGGER.warning("Failed to delete temp file %s", path, exc_info=True)


def _read_pack_json(zip_path: Path, name: str) -> Optional[Any]:
    with zipfile.ZipFile(zip_path) as zf:
        if name not in zf.namelist():
            return None
        return json.loads(zf.read(name).decode("utf-8"))


def _read_manifest(zip_path: Path) -> Dict[str, Any]:
    """ZIP の manifest.json を読み、形式名だけ確かめる (中身の検査は importer が行う)。"""
    from saiverse_memory.persona_pack import FORMAT_NAME, MANIFEST_NAME

    try:
        manifest = _read_pack_json(zip_path, MANIFEST_NAME)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="The file is not a ZIP archive.")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"manifest.json could not be read: {exc}")
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=400, detail="This ZIP is not a persona pack (manifest.json is missing).")
    if manifest.get("format") != FORMAT_NAME:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format: {manifest.get('format')!r} (expected {FORMAT_NAME!r})",
        )
    return manifest


def _safe_filename_part(text: str) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", text).strip().strip(".")
    return cleaned or "persona"


# ---------------------------------------------------------------------------
# エクスポート
# ---------------------------------------------------------------------------

@router.get("/{persona_id}/persona-pack")
def export_persona_pack_endpoint(persona_id: str, manager=Depends(get_manager)):
    """ペルソナ一体をペルソナまるごとパック (ZIP) にしてダウンロードさせる。読み取り専用。"""
    from saiverse_memory.persona_pack import MANIFEST_NAME, export_persona_pack

    ensure_persona_exists(persona_id, manager)
    saiverse_db_path, saiverse_home = _world_paths(manager)
    tmp_dir = Path(tempfile.mkdtemp(prefix="persona_pack_export_"))
    output = tmp_dir / "persona_pack.zip"
    try:
        export_persona_pack(
            persona_id,
            saiverse_db_path=saiverse_db_path,
            saiverse_home=saiverse_home,
            output_path=output,
        )
        manifest = _read_pack_json(output, MANIFEST_NAME) or {}
    except ValueError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        LOGGER.exception("Persona pack export failed for %s", persona_id)
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}")

    name = manifest.get("persona_name") if isinstance(manifest, dict) else None
    stem = _safe_filename_part(str(name or persona_id))
    filename = f"{stem}_persona_pack_{datetime.now().strftime('%Y%m%d')}.zip"
    return FileResponse(
        output,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True),
    )


# ---------------------------------------------------------------------------
# インポートの共通処理
# ---------------------------------------------------------------------------

def _start_embedding(persona_id: str, count: int) -> bool:
    """取り込んだ message の埋め込みを裏で作り直す (import_chatlog.py と同じ経路)。

    起動できなくても取り込みは成功のまま — 空のベクトル行は残り、起動時の検査と
    再生成のボタンが拾う。
    """
    from saiverse_memory import SAIMemoryAdapter

    from .import_chatlog import _launch_import_embedding

    try:
        adapter = SAIMemoryAdapter(persona_id)
    except Exception:
        LOGGER.exception("Could not open SAIMemory for the import embedding of %s", persona_id)
        return False
    if not _launch_import_embedding(persona_id, adapter, count):
        adapter.close()
        return False
    return True


def _refresh_loaded_persona_profile(manager: Any, persona_id: str) -> None:
    """取り込みで書き換えた ai 行を、読み込み済みのペルソナへ保存経路で写す。

    manager.update_ai に DB の現在値をそのまま渡す (値は変わらず、インメモリの名前・
    システムプロンプト・言語・モデル・アイコンが写る)。自律行動の ON/OFF は
    update_ai が「DB の値が変わったとき」だけ同期するので、ここで同期させる。
    """
    personas = getattr(manager, "personas", None) or {}
    if persona_id not in personas:
        return
    from database.models import AI

    db = manager.SessionLocal()
    try:
        ai = db.query(AI).filter(AI.AIID == persona_id).first()
        if ai is None:
            return
        values = dict(
            name=ai.AINAME,
            description=ai.DESCRIPTION,
            system_prompt=ai.SYSTEMPROMPT,
            home_city_id=ai.HOME_CITYID,
            default_model=ai.DEFAULT_MODEL,
            lightweight_model=ai.LIGHTWEIGHT_MODEL,
            autonomy_enabled=bool(ai.AUTONOMY_ENABLED),
            avatar_path=ai.AVATAR_IMAGE,
            appearance_image_path=ai.APPEARANCE_IMAGE_PATH or "",
            vision_model=ai.VISION_MODEL,
            audio_model=ai.AUDIO_MODEL,
            video_model=ai.VIDEO_MODEL,
            memory_weave_model=ai.MEMORY_WEAVE_MODEL,
            reflex_judgment_model=ai.REFLEX_JUDGMENT_MODEL,
            language=ai.LANGUAGE or "",
        )
    finally:
        db.close()
    try:
        message = manager.update_ai(
            persona_id,
            values["name"],
            values["description"],
            values["system_prompt"],
            values["home_city_id"],
            values["default_model"],
            values["lightweight_model"],
            values["autonomy_enabled"],
            values["avatar_path"],
            None,
            values["appearance_image_path"],
            vision_model=values["vision_model"],
            audio_model=values["audio_model"],
            video_model=values["video_model"],
            memory_weave_model=values["memory_weave_model"],
            reflex_judgment_model=values["reflex_judgment_model"],
            language=values["language"],
        )
        if isinstance(message, str) and message.startswith("Error"):
            LOGGER.warning("Persona pack: could not apply the profile to %s: %s", persona_id, message)
        ensure_autonomy = getattr(manager, "ensure_autonomy_for", None)
        if callable(ensure_autonomy):
            ensure_autonomy(persona_id)
    except Exception:
        LOGGER.exception("Persona pack: could not apply the profile to the loaded persona %s", persona_id)


def _chronicle_after_merge(
    manager: Any, persona_id: str, saiverse_home: Path,
) -> Dict[str, Any]:
    """合流で Chronicle ページを取り込んだ後の処理と、次の編纂の予測。

    1. 冷えた anchor 行の窓を覆う取り込んだ記事へ presented_raw の印を書く
       (sea.coverage_repair.mark_covered_cold_windows — 補修の完了処理と同じ
       関数・同じ呼び方)。head の再取得後に、窓の生ログと head のあらすじ枠に
       同じ記事が二重に出るのを防ぐ。補修と同じく、読み込み済みのペルソナ
       (adapter が使える) でなければ書かない。
       補修の出口が行う head の再構築 (_dispatch_manual_head_rebuild) は行わない:
       補修の記事は止め線 (温かい anchor の最古位置) より古い範囲だけを覆うが、
       合流の記事にはその保証が無く、温かい窓の中のメッセージを覆いうる。
       温かい行には印を書かない (arasuji_levels.md §16-2) ので、ここで head を組み直すと、その記事が
       すぐに温かい窓の生ログと二重に出る。
    2. 次の編纂で走る束ねの回数 (bands.plan_band_overflow、LLM なしの予測) と、
       content_stale の印を持つ上位の件数を数える。
    """
    out: Dict[str, Any] = {
        "cold_windows_marked": None,
        "cold_window_mark_failures": None,
        "cold_window_marks_skipped": False,
        "fold_runs_predicted": None,
        "stale_entries": None,
    }
    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    lifecycle = getattr(getattr(manager, "sea_runtime", None), "session_lifecycle", None)
    if persona is None or lifecycle is None:
        out["cold_window_marks_skipped"] = True
        LOGGER.info(
            "Persona pack merge: %s is not loaded; cold-window marks were not written", persona_id,
        )
    else:
        try:
            from sea.coverage_repair import mark_covered_cold_windows
            marked, failed = mark_covered_cold_windows(lifecycle, persona)
        except Exception:
            LOGGER.warning(
                "Persona pack merge: cold-window marking failed (persona=%s)", persona_id,
                exc_info=True,
            )
            marked, failed = 0, -1  # 数不明の全滅 (run_coverage_repair と同じ表し方)
        out["cold_windows_marked"] = marked
        out["cold_window_mark_failures"] = failed

    from sai_memory.arasuji.absorption import list_stale_upper_ids
    from sai_memory.arasuji.bands import plan_band_overflow
    from sai_memory.arasuji.storage import init_arasuji_tables

    conn = sqlite3.connect(str(saiverse_home / "personas" / persona_id / "memory.db"))
    try:
        init_arasuji_tables(conn)
        out["fold_runs_predicted"] = plan_band_overflow(conn)
        out["stale_entries"] = len(list_stale_upper_ids(conn))
    except Exception:
        LOGGER.warning(
            "Persona pack merge: could not predict the next Chronicle run (persona=%s)",
            persona_id, exc_info=True,
        )
    finally:
        conn.close()
    return out


def _run_import(
    manager: Any, zip_path: Path, *, mode: str, persona_id: str, overwrite_profile: bool,
) -> Dict[str, Any]:
    """import_persona_pack を呼び、後処理 (プロフィールの反映・埋め込みの再生成) をする。

    合流は Beat ロック (sea.beat_gate.hold_beat) の内側で行う — 会話・Metabolism・
    あらすじの補修ジョブは同じロックの内側で記憶に書くので、取り込みとその後の
    印の書き込みがそれらと並走しない (補修・あらすじ削除と同じく check_gate=False)。
    """
    from saiverse_memory.persona_pack import import_persona_pack

    saiverse_db_path, saiverse_home = _world_paths(manager)

    def run() -> Any:
        return import_persona_pack(
            zip_path,
            mode=mode,
            target_persona_id=persona_id,
            saiverse_db_path=saiverse_db_path,
            saiverse_home=saiverse_home,
            overwrite_profile=overwrite_profile,
        )

    chronicle_after_merge: Optional[Dict[str, Any]] = None
    if mode == "merge":
        from sea.beat_gate import hold_beat

        with hold_beat(manager, persona_id, purpose="persona_pack_merge", check_gate=False):
            result = run()
            chronicle_counts = result.counts.get("chronicle_pages")
            if chronicle_counts is not None and chronicle_counts.added:
                chronicle_after_merge = _chronicle_after_merge(manager, persona_id, saiverse_home)
    else:
        result = run()
    if result.profile_columns_overwritten:
        _refresh_loaded_persona_profile(manager, persona_id)
    embedding_started = False
    if result.embeddings_deferred:
        embedding_started = _start_embedding(persona_id, result.embeddings_deferred)
    payload = asdict(result)
    payload["embedding_started"] = embedding_started
    payload["chronicle_after_merge"] = chronicle_after_merge
    return payload


def _import_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, zipfile.BadZipFile):
        return HTTPException(status_code=400, detail="The file is not a ZIP archive.")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    LOGGER.exception("Persona pack import failed")
    return HTTPException(status_code=500, detail=f"Import failed: {exc}")


# ---------------------------------------------------------------------------
# 合流 (既存のペルソナへ)
# ---------------------------------------------------------------------------

@router.post("/{persona_id}/persona-pack/import")
async def import_persona_pack_merge(
    persona_id: str,
    file: UploadFile = File(...),
    overwrite_profile: bool = Form(False),
    manager=Depends(get_manager),
):
    """パックを既存のペルソナへ合流させる (intent §5)。結果の内訳を返す。

    このペルソナのあらすじ生成・補修のジョブが終端していなければ 409 で断る
    (合流は Chronicle の木に書くので、編纂と並走させない)。判定の後に始まった
    ジョブとは、_run_import の Beat ロックで直列になる。
    """
    from .arasuji import has_active_generation_job

    ensure_persona_exists(persona_id, manager)
    if has_active_generation_job(persona_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "A Chronicle generation or repair job is running for this persona. "
                "Wait for it to finish, then import again."
            ),
        )
    with _claim_import(persona_id):
        zip_path = await _save_upload(file)
        try:
            _read_manifest(zip_path)
            return await run_in_threadpool(
                _run_import, manager, zip_path,
                mode="merge", persona_id=persona_id, overwrite_profile=overwrite_profile,
            )
        except Exception as exc:
            raise _import_error(exc)
        finally:
            _remove(zip_path)


# ---------------------------------------------------------------------------
# 新しいペルソナとして迎える
# ---------------------------------------------------------------------------

def _pick_ai_stem(manager: Any, name: str) -> Optional[str]:
    """名前から AIID の名前部分を選ぶ。同じ ID が既にあれば番号を足す (intent §5)。

    英数字が残らない名前 (日本語名など) は None — 作成経路が persona_<連番> を選ぶ。
    """
    from sqlalchemy import func

    from database.models import AI
    from manager.ids import is_valid_identifier, slugify_identifier

    slug = slugify_identifier(name)
    if not is_valid_identifier(slug):
        return None
    city = str(manager.city_name)
    db = manager.SessionLocal()
    try:
        def taken(stem: str) -> bool:
            ai_id = f"{stem}_{city}".lower()
            return db.query(AI.AIID).filter(func.lower(AI.AIID) == ai_id).first() is not None

        stem, n = slug, 2
        while taken(stem):
            stem = f"{slug}_{n}"
            n += 1
        return stem
    finally:
        db.close()


def _name_taken(manager: Any, name: str) -> bool:
    """作成経路の名前の重複検査 (同じ街に同名のペルソナ。大文字小文字を畳む) と同じ判定。"""
    from sqlalchemy import func

    from database.models import AI

    db = manager.SessionLocal()
    try:
        return (
            db.query(AI.AIID)
            .filter(AI.HOME_CITYID == manager.city_id, func.lower(AI.AINAME) == name.lower())
            .first()
            is not None
        )
    finally:
        db.close()


def _memory_has_messages(saiverse_home: Path, persona_id: str) -> bool:
    db_path = saiverse_home / "personas" / persona_id / "memory.db"
    if not db_path.is_file():
        return False
    conn = sqlite3.connect(str(db_path))
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return bool(count)
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _create_and_import(manager: Any, zip_path: Path, name: str, system_prompt: str) -> Dict[str, Any]:
    """既存のペルソナ作成経路で新しいペルソナ (私室含む) を作り、mode="new" で取り込む。

    作成の後で取り込みが失敗したとき:
    - memory.db に message が入っていなければ (取り込みが丸ごと巻き戻った)、作った
      ペルソナを既存の削除経路 (manager.delete_ai) で消す。
    - 入っていれば (memory.db の commit 後に saiverse.db の commit が失敗した稀な回)、
      ペルソナは残して ID を知らせる — 同じパックを合流で取り込み直せば残りが入る
      (importer.py の docstring)。
    """
    stem = _pick_ai_stem(manager, name)
    success, message, ai_id, room_id = manager.create_ai(
        name, system_prompt, manager.city_id, stem,
    )
    if not success or not ai_id:
        raise HTTPException(status_code=400, detail=message or "Failed to create the persona.")
    try:
        payload = _run_import(manager, zip_path, mode="new", persona_id=ai_id, overwrite_profile=True)
    except Exception as exc:
        _, saiverse_home = _world_paths(manager)
        if _memory_has_messages(saiverse_home, ai_id):
            LOGGER.error(
                "Persona pack import into the new persona %s failed after its memory was "
                "written; the persona is kept", ai_id, exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    f"The persona '{ai_id}' was created and its memories were imported, but the "
                    f"rest of the import failed ({exc}). Import the same pack again with "
                    "'merge' into this persona to fill in the rest."
                ),
            )
        removed = manager.delete_ai(ai_id)
        LOGGER.warning(
            "Persona pack import into the new persona %s failed; removed it (%s)", ai_id, removed,
            exc_info=True,
        )
        raise _import_error(exc)
    payload["persona_id"] = ai_id
    payload["room_id"] = room_id
    payload["persona_name"] = name
    return payload


@router.post("/persona-pack/import-new")
async def import_persona_pack_new(
    file: UploadFile = File(...),
    manager=Depends(get_manager),
):
    """パックから新しいペルソナを迎える (intent §5 の新規作成)。結果の内訳を返す。"""
    zip_path = await _save_upload(file)
    try:
        manifest = _read_manifest(zip_path)
        name = manifest.get("persona_name")
        if not isinstance(name, str) or not name.strip():
            name = manifest.get("persona_id")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(status_code=400, detail="manifest.json has no persona name.")
        name = name.strip()
        if _name_taken(manager, name):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A persona named '{name}' already exists in this city. To combine the pack "
                    "with that persona, import it from that persona's memory screen instead."
                ),
            )
        system_prompt = ""
        try:
            profile = _read_pack_json(zip_path, "profile/profile.json")
            if isinstance(profile, dict) and isinstance(profile.get("ai"), dict):
                value = profile["ai"].get("SYSTEMPROMPT")
                if isinstance(value, str):
                    system_prompt = value
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass  # importer が形を検査して断る
        return await run_in_threadpool(_create_and_import, manager, zip_path, name, system_prompt)
    except Exception as exc:
        raise _import_error(exc)
    finally:
        _remove(zip_path)
