"""ペルソナまるごとパック (persona-pack) の API。

正典: docs/intent/persona_pack.md。エンジンは saiverse_memory/persona_pack/
(export_persona_pack / import_persona_pack)。ここはそれを本体サーバーへ配線する:

- ``GET  /{persona_id}/persona-pack``                — 書き出し (読み取り専用)。ZIP を返す。
- ``POST /{persona_id}/persona-pack/import``         — 既存のペルソナへ合流 (mode="merge")。
- ``POST /persona-pack/import-new``                  — 新しいペルソナを作って取り込む (mode="new")。

取り込みは同期で行い、結果の内訳 (PersonaPackImportResult) を JSON で返す。
合流と新規作成は、ペルソナを問わずプロセス内で一度に一つだけ走らせる (_claim_import)。
取り込んだ message は空のベクトル行を持って入る (importer.py の docstring) ので、
import_chatlog.py と同じ ``_launch_import_embedding`` で埋め込みの再生成を裏で起こす。
起こすかどうかは、取り込み後の memory.db に残る空のベクトル行の件数で決める
(_deferred_embedding_count) — 印による再開の合流は足す message が 0 件でも、止まった
回が残した空のベクトル行がある。生きているペルソナの adapter は import_chatlog.py と同じく触らない (取り込みは別の
接続で memory.db に書き、埋め込みは使い捨ての adapter が行う)。プロフィールを上書きした
ときだけ、読み込み済みのペルソナへ保存経路 (manager.update_ai) で値を写す。私室の設定と
所持品を取り込んだときは、読み込み済みの Building と所持品の一覧へ写す
(_refresh_loaded_room_and_items)。

合流は Beat ロックの内側で行い、あらすじ生成・補修のジョブが終端していなければ
409 で断る。Chronicle ページを取り込んだ合流では、冷えた窓への presented_raw の印と
次の編纂の予測 (束ねの回数・content_stale の件数) を結果の ``chronicle_after_merge``
に載せる (_chronicle_after_merge)。
"""
from __future__ import annotations

import hashlib
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

# 取り込み中の key (_claim_import)。_ANY_IMPORT_KEY は種類を問わず取り込みが一つ
# 走っている間だけ入る
_importing: set = set()
_importing_lock = threading.Lock()
_ANY_IMPORT_KEY = "*"

# ファイル名に使えない文字 (Windows の禁止文字と制御文字)
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _world_paths(manager: Any) -> tuple[Path, Path]:
    """取り込み先 / 書き出し元の saiverse.db と SAIVERSE_HOME。"""
    return Path(manager.db_path), Path(manager.saiverse_home)


@contextmanager
def _claim_import(
    key: str, detail: str = "A persona pack import is already running for this persona.",
) -> Iterator[None]:
    """取り込み (合流と新規作成) をプロセス内で一度に一つだけ走らせる (二つ目は 409)。

    同じ key (同じペルソナへの合流・同じパックの新規作成) が走っていれば detail で、
    別の key の取り込みが走っていれば「別の取り込みが走っている」旨で断る。

    key 別だけでなく全体を一つに絞る理由: 取り込みは共有メディアフォルダへ添付を
    書き、同じ中身のファイルが既にあれば再利用する (importer.py の choose_media_dest)。
    別のペルソナ宛ての取り込み A と B が並走すると、B が A の書いたまだ確定していない
    ファイルを再利用し、A が失敗して自分の書いたファイルを消す
    (remove_created_files) と B の参照が欠ける。引っ越しは稀な操作なので、全部を
    直列にする性能の損は受け入れる。

    プロセス内のロック (_importing_lock) なので、SAIVerse を単一プロセスで動かす
    運用 (main.py が一つのサーバープロセスを起動する) を前提にしている。
    """
    with _importing_lock:
        if key in _importing:
            raise HTTPException(status_code=409, detail=detail)
        if _ANY_IMPORT_KEY in _importing:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Another persona pack import is running. Wait for it to finish, "
                    "then import again."
                ),
            )
        _importing.update((key, _ANY_IMPORT_KEY))
    try:
        yield
    finally:
        with _importing_lock:
            _importing.discard(key)
            _importing.discard(_ANY_IMPORT_KEY)


async def _save_upload(file: UploadFile) -> Path:
    """アップロードを一時ファイルへ書く (呼び出し側が消す)。書けなければ消してから投げる。"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip", prefix="persona_pack_") as tmp:
        tmp_path = Path(tmp.name)
    tmp_path.unlink()
    try:
        await write_upload_bounded(file, tmp_path, PERSONA_PACK_IMPORT_MAX_BYTES)
    except BaseException:
        _remove(tmp_path)
        raise
    return tmp_path


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        LOGGER.warning("Failed to delete temp file %s", path, exc_info=True)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_pack_json(zip_path: Path, name: str) -> Optional[Any]:
    with zipfile.ZipFile(zip_path) as zf:
        if name not in zf.namelist():
            return None
        return json.loads(zf.read(name).decode("utf-8"))


def _read_manifest(zip_path: Path) -> Dict[str, Any]:
    """ZIP の manifest.json を読み、形式名だけ確かめる (中身の検査は importer が行う)。

    manifest を展開する前に、展開後のサイズの上限 (importer.check_uncompressed_size —
    合計・1 エントリ・JSON の合計) を確かめる。
    """
    from saiverse_memory.persona_pack import FORMAT_NAME, MANIFEST_NAME
    from saiverse_memory.persona_pack.importer import check_uncompressed_size

    try:
        with zipfile.ZipFile(zip_path) as zf:
            check_uncompressed_size(zf)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="The file is not a ZIP archive.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
    # _launch_import_embedding が True を返したときだけ、スレッドが adapter を引き取る
    # (閉じるのもスレッド側)。False と例外のときはここで閉じる。
    launched = False
    try:
        launched = _launch_import_embedding(persona_id, adapter, count)
    finally:
        if not launched:
            try:
                adapter.close()
            except Exception:
                LOGGER.warning("Could not close the SAIMemory adapter of %s", persona_id, exc_info=True)
    return launched


def _deferred_embedding_count(saiverse_home: Path, persona_id: str) -> int:
    """ペルソナの memory.db で埋め込みを待つ message の件数 (空のベクトル行 DEFERRED_VECTOR)。

    数え方は起動時の検査 (SAIMemoryAdapter の count_deferred_message_embeddings) と同じ。
    取り込みの後に起こす _start_embedding の SAIMemoryAdapter.reembed_stale_messages は
    この行を全部拾うので、今回の取り込みが書いた行かどうかは問わない。
    """
    from sai_memory.memory.storage import count_deferred_message_embeddings

    db_path = saiverse_home / "personas" / persona_id / "memory.db"
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        return count_deferred_message_embeddings(conn)
    finally:
        conn.close()


def _read_spell_enabled(manager: Any, persona_id: str) -> Optional[bool]:
    """読み込み済みのペルソナの SPELL_ENABLED を DB から読む。

    読み込まれていない・行が無い・読めなかったときは None (切り替えを届けない)。
    読めないことで取り込みそのものは止めない。
    """
    if persona_id not in (getattr(manager, "personas", None) or {}):
        return None
    from database.models import AI

    try:
        db = manager.SessionLocal()
        try:
            ai = db.query(AI).filter(AI.AIID == persona_id).first()
            return None if ai is None else bool(ai.SPELL_ENABLED)
        finally:
            db.close()
    except Exception:
        LOGGER.warning(
            "Persona pack: could not read SPELL_ENABLED of %s before the import", persona_id,
            exc_info=True,
        )
        return None


def _refresh_loaded_persona_profile(
    manager: Any, persona_id: str, spell_enabled_before: Optional[bool] = None,
) -> None:
    """取り込みで書き換えた ai 行を、読み込み済みのペルソナへ保存経路で写す。

    manager.update_ai に DB の現在値をそのまま渡す (値は変わらず、インメモリの名前・
    システムプロンプト・言語・モデル・アイコンが写る)。自律行動の ON/OFF は
    update_ai が「DB の値が変わったとき」だけ同期するので、ここで同期させる。

    スペル不使用モード (SPELL_ENABLED) の切り替えも update_ai は「DB の旧値と
    違うとき」だけ head へ届ける (docs/intent/spell_disabled_mode.md §4-2) が、
    取り込みは DB を書き終えているので旧値は残っていない。そこで呼び出し側が
    取り込みの前に読んだ値 ``spell_enabled_before`` と DB の現在値を比べ、違えば
    通常の保存と同じ AdminService._deliver_spell_toggle を、update_ai の後
    (モデルの当てはめが済んだ後) に呼ぶ。
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
        spell_enabled_now = bool(ai.SPELL_ENABLED)
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
            name=values["name"],
            description=values["description"],
            system_prompt=values["system_prompt"],
            home_city_id=values["home_city_id"],
            default_model=values["default_model"],
            lightweight_model=values["lightweight_model"],
            autonomy_enabled=values["autonomy_enabled"],
            avatar_path=values["avatar_path"],
            avatar_upload=None,
            appearance_image_path=values["appearance_image_path"],
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
    # update_ai が失敗した回でも DB のモードは取り込みで確定しているので届ける
    # (update_ai の例外経路が commit 後の切り替えを届けるのと同じ扱い)。
    if spell_enabled_before is None or spell_enabled_before == spell_enabled_now:
        return
    deliver = getattr(getattr(manager, "admin", None), "_deliver_spell_toggle", None)
    persona = personas.get(persona_id)
    if callable(deliver) and persona is not None:
        deliver(persona_id, persona)


def _refresh_loaded_room_and_items(manager: Any, persona_id: str) -> None:
    """取り込みで書き換えた私室の設定と登録した所持品を、読み込み済みの世界へ写す。

    私室は manager.update_building が保存後にインメモリの Building へ写す値
    (名前・定員・説明・システム指示・間隔・アイテムの表示上限) を DB の現在値で写す。
    所持品はアイテム作成と同じく manager._load_items_from_db で読み直し、私室の
    システム指示に所持品の一覧を載せ直す。manager がその口を持たなければ何もしない。
    """
    from database.models import AI, Building

    building_map = getattr(manager, "building_map", None)
    db = manager.SessionLocal()
    try:
        ai = db.query(AI).filter(AI.AIID == persona_id).first()
        room_id = ai.PRIVATE_ROOM_ID if ai is not None else None
        row = db.query(Building).filter(Building.BUILDINGID == room_id).first() if room_id else None
        values = None
        if row is not None:
            values = dict(
                name=row.BUILDINGNAME, capacity=row.CAPACITY, description=row.DESCRIPTION,
                system_instruction=row.SYSTEM_INSTRUCTION, interval=row.AUTO_INTERVAL_SEC,
                item_display_limit=row.ITEM_DISPLAY_LIMIT,
            )
    finally:
        db.close()
    if values is not None and isinstance(building_map, dict) and room_id in building_map:
        building = building_map[room_id]
        building.name = values["name"]
        building.capacity = values["capacity"]
        building.description = values["description"]
        building.base_system_instruction = values["system_instruction"]
        building.system_instruction = values["system_instruction"]
        building.auto_interval_sec = values["interval"]
        building.item_display_limit = values["item_display_limit"]
        capacities = getattr(manager, "capacities", None)
        if isinstance(capacities, dict) and room_id in capacities:
            capacities[room_id] = values["capacity"]
    load_items = getattr(manager, "_load_items_from_db", None)
    if callable(load_items):
        load_items()
        refresh = getattr(manager, "_refresh_building_system_instruction", None)
        if callable(refresh) and room_id:
            refresh(room_id)


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

    返す値の意味 (frontend の PersonaPackChronicleAfterMerge と同じ):
    ``cold_window_mark_failures`` は 0 = 全部書けた、正の数 = 書けなかった行の数、
    -1 = 数の分からない失敗 (印の処理が例外を出した)、None = 印を書いたか
    分からない (``cold_window_marks_skipped`` が True = 読み込まれていないので
    書かなかった、または _run_import がこの関数ごと失敗した)。
    ``fold_runs_predicted`` / ``stale_entries`` の None は予測できなかった。
    """
    out = _unknown_chronicle_after_merge()
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

    conn: Optional[sqlite3.Connection] = None
    try:
        from sai_memory.arasuji.absorption import list_stale_upper_ids
        from sai_memory.arasuji.bands import plan_band_overflow
        from sai_memory.arasuji.storage import init_arasuji_tables

        conn = sqlite3.connect(str(saiverse_home / "personas" / persona_id / "memory.db"))
        init_arasuji_tables(conn)
        out["fold_runs_predicted"] = plan_band_overflow(conn)
        out["stale_entries"] = len(list_stale_upper_ids(conn))
    except Exception:
        LOGGER.warning(
            "Persona pack merge: could not predict the next Chronicle run (persona=%s)",
            persona_id, exc_info=True,
        )
    finally:
        if conn is not None:
            conn.close()
    return out


def _unknown_chronicle_after_merge() -> Dict[str, Any]:
    """_chronicle_after_merge の結果の初期値 (どの値もまだ分からない)。"""
    return {
        "cold_windows_marked": None,
        "cold_window_mark_failures": None,
        "cold_window_marks_skipped": False,
        "fold_runs_predicted": None,
        "stale_entries": None,
    }


def _after_commit(result: Any, step: str, func: Any, *args: Any) -> Any:
    """取り込みの commit 後の処理を一つ走らせる。失敗は result.warnings に載せて None を返す。

    commit 後の失敗を HTTP エラーにすると、取り込み済みなのにクライアントが
    失敗と受け取って同じパックを再送する (二重取り込みの誘因) ので、ここで止める。
    """
    try:
        return func(*args)
    except Exception as exc:
        LOGGER.exception("Persona pack import: %s failed after the import was committed", step)
        result.warnings.append(
            f"The import was saved, but {step} failed: {type(exc).__name__}: {exc}".splitlines()[0]
        )
        return None


def _run_import(
    manager: Any, zip_path: Path, *, mode: str, persona_id: str, overwrite_profile: bool,
) -> Dict[str, Any]:
    """import_persona_pack を呼び、後処理 (プロフィールの反映・埋め込みの再生成) をする。

    合流は Beat ロック (sea.beat_gate.hold_beat) の内側で行う — 会話・Metabolism・
    あらすじの補修ジョブは同じロックの内側で記憶に書くので、取り込みとその後の
    印の書き込みがそれらと並走しない (補修・あらすじ削除と同じく check_gate=False)。

    import_persona_pack が返った後 (commit 済み) の処理は、失敗しても例外を投げず、
    結果の warnings に載せて返す (_after_commit)。
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
    # プロフィール上書きの合流は SPELL_ENABLED を DB へ直接書くので、旧値を書く前に
    # 控える (_refresh_loaded_persona_profile が切り替えを届けるかの判定に使う)。
    # 新規作成のペルソナは読み込まれておらず、起動時の head の構築が新しい値を読む。
    spell_enabled_before: Optional[bool] = None
    if mode == "merge":
        from sea.beat_gate import hold_beat

        with hold_beat(manager, persona_id, purpose="persona_pack_merge", check_gate=False):
            if overwrite_profile:
                spell_enabled_before = _read_spell_enabled(manager, persona_id)
            result = run()
            chronicle_counts = result.counts.get("chronicle_pages")
            if chronicle_counts is not None and chronicle_counts.added:
                chronicle_after_merge = _after_commit(
                    result, "the Chronicle follow-up (cold-window marks and forecast)",
                    _chronicle_after_merge, manager, persona_id, saiverse_home,
                )
                if chronicle_after_merge is None:
                    chronicle_after_merge = _unknown_chronicle_after_merge()
    else:
        result = run()
    if result.profile_columns_overwritten:
        _after_commit(
            result, "applying the profile to the loaded persona",
            _refresh_loaded_persona_profile, manager, persona_id, spell_enabled_before,
        )
    item_counts = result.counts.get("items")
    if item_counts is not None and (item_counts.added or item_counts.updated):
        _after_commit(
            result, "applying the private room and items to the loaded world",
            _refresh_loaded_room_and_items, manager, persona_id,
        )
    # 起動の判断は今回足した件数 (embeddings_deferred) ではなく memory.db に残る
    # 空のベクトル行の件数で行う — 印による再開の合流は足す件数が 0 でも、
    # 止まった回が残した空のベクトル行を埋める必要がある (_deferred_embedding_count)
    embedding_started = False
    deferred = _after_commit(
        result, "counting the messages waiting for their embeddings",
        _deferred_embedding_count, saiverse_home, persona_id,
    )
    if deferred:
        embedding_started = bool(_after_commit(
            result, "starting the embedding of the imported messages",
            _start_embedding, persona_id, deferred,
        ))
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


def _memory_has_messages(saiverse_home: Path, persona_id: str) -> Optional[bool]:
    """ペルソナの memory.db に message があるか。

    False = 空と確定 (memory.db が無い、または messages が 0 行)、True = ある、
    None = 確かめられなかった (開けない・ロック・表が無いなど)。
    """
    db_path = saiverse_home / "personas" / persona_id / "memory.db"
    try:
        if not db_path.is_file():
            return False
        conn = sqlite3.connect(str(db_path), timeout=5)
    except (OSError, sqlite3.Error):
        LOGGER.warning("Could not open the memory of %s to check it", persona_id, exc_info=True)
        return None
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return bool(count)
    except sqlite3.Error:
        LOGGER.warning("Could not count the messages of %s", persona_id, exc_info=True)
        return None
    finally:
        conn.close()


def _discard_created_persona(manager: Any, ai_id: str, room_id: Optional[str]) -> list[str]:
    """新規取り込みが丸ごと巻き戻ったとき、この取り込みで作ったペルソナと私室を消す。

    作成経路 (manager/persona.py の _create_persona) が登録するものと、ここでの外し方:

    - saiverse.db: ai 行 (manager.delete_ai)、私室の building 行・私室の
      building_occupancy_log の行・user_ai_link の行 (ここで消す)。私室の fixture 行は
      作成経路は作らず、取り込みの items/ の工程が入れる (取り込みの rollback で戻る) が、
      残っていれば observer_manager.delete_fixture_rows でぶら下がる行ごと消す。
    - manager のメモリ: personas / persona_map / id_to_name_map / avatar_map と
      occupants の中の ai_id (manager.delete_ai が外す)、buildings / building_map /
      capacities / occupants / building_memory_paths / building_histories の私室の項目
      (ここで外す)、_autonomy_managers の項目 (_on_persona_registered の
      ensure_autonomy_for が起動しうる。ここで止めて外す)。

    delete_ai は私室を消さないので、私室を残すと同じパックの再試行が
    building の UNIQUE(CITYID, BUILDINGNAME) に衝突する。

    Returns: 片付けられなかったものの説明 (全部済めば空)。
    """
    problems: list[str] = []
    autonomy_managers = getattr(manager, "_autonomy_managers", None)
    if isinstance(autonomy_managers, dict):
        am = autonomy_managers.pop(ai_id, None)
        if am is not None:
            try:
                am.stop()
            except Exception:
                LOGGER.warning("Could not stop the AutonomyManager of %s", ai_id, exc_info=True)

    removed = manager.delete_ai(ai_id)
    if isinstance(removed, str) and removed.startswith("Error"):
        # ai 行が残る間は私室も残す (ai.PRIVATE_ROOM_ID が指す先を消さない)
        problems.append(f"the persona '{ai_id}' could not be deleted ({removed})")
        return problems
    if not room_id:
        return problems

    from database.models import Building, BuildingOccupancyLog, Fixture, UserAiLink
    from saiverse.observer_manager import cancel_observer_jobs, delete_fixture_rows

    observer_ids: list[str] = []
    db = manager.SessionLocal()
    try:
        fixture_ids = [
            row[0] for row in db.query(Fixture.FIXTURE_ID).filter(Fixture.BUILDING_ID == room_id).all()
        ]
        observer_ids = delete_fixture_rows(db, fixture_ids)
        db.query(BuildingOccupancyLog).filter(BuildingOccupancyLog.BUILDINGID == room_id).delete(
            synchronize_session=False
        )
        db.query(UserAiLink).filter(UserAiLink.AIID == ai_id).delete(synchronize_session=False)
        db.query(Building).filter(Building.BUILDINGID == room_id).delete(synchronize_session=False)
        db.commit()
    except Exception as exc:
        db.rollback()
        LOGGER.error("Could not delete the private room %s of %s", room_id, ai_id, exc_info=True)
        problems.append(f"the private room '{room_id}' could not be deleted ({exc})")
        return problems
    finally:
        db.close()
    try:
        cancel_observer_jobs(manager, observer_ids)
    except Exception:
        LOGGER.warning("Could not cancel the observer jobs of the room %s", room_id, exc_info=True)

    buildings = getattr(manager, "buildings", None)
    if isinstance(buildings, list):
        # 一覧は PersonaCore などと共有されているので、差し替えずにその場で外す
        buildings[:] = [b for b in buildings if getattr(b, "building_id", None) != room_id]
    for attr in ("building_map", "capacities", "occupants", "building_memory_paths", "building_histories"):
        table = getattr(manager, attr, None)
        if isinstance(table, dict):
            table.pop(room_id, None)
    return problems


def _create_and_import(manager: Any, zip_path: Path, name: str, system_prompt: str) -> Dict[str, Any]:
    """既存のペルソナ作成経路で新しいペルソナ (私室含む) を作り、mode="new" で取り込む。

    作成の前に importer.validate_pack でパック全体を (書き込みなしで) 検査する — 壊れた
    JSON・形式違反・サイズ超過は、ペルソナを作る前に 400 で返る。

    作成の後で取り込みが失敗したとき:
    - memory.db の commit 後に saiverse.db の commit が失敗した稀な回 (ペルソナフォルダに
      取り込み途中の印 WORLD_PENDING_MARKER が残る、または message が入っている) は、
      ペルソナを残して ID を知らせる — 同じパックをこのペルソナへ合流で取り込むと、
      印を見て saiverse.db 側の工程 (タスク・予定・イベント履歴・所持品・私室など) を
      やり直す (importer.py の docstring「片側だけ commit された取り込みの再開」)。
    - それ以外で memory.db に message が無いと確定したら (取り込みが丸ごと巻き戻った)、
      作ったペルソナと私室を _discard_created_persona で消す (同じパックで再試行できる)。
    - 確かめられなければ (memory.db を開けない・ロックなど)、取り込みが部分的に
      残っている可能性があるので消さずに残し、同じく ID を知らせる。
    """
    from saiverse_memory.persona_pack.export import WORLD_PENDING_MARKER
    from saiverse_memory.persona_pack.importer import validate_pack

    validate_pack(zip_path)
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
        pending = (saiverse_home / "personas" / ai_id / WORLD_PENDING_MARKER).is_file()
        has_messages = _memory_has_messages(saiverse_home, ai_id)
        if pending or has_messages is True:
            LOGGER.error(
                "Persona pack import into the new persona %s failed after its memory was "
                "written; the persona is kept", ai_id, exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    f"The persona '{ai_id}' was created and its memories were imported, but the "
                    f"rest of the import failed ({exc}). Import the same pack again with "
                    "'merge' into this persona: the merge finishes the unfinished part "
                    "(tasks, schedules, event log, items and private room)."
                ),
            )
        if has_messages is None:
            LOGGER.error(
                "Persona pack import into the new persona %s failed and its memory could not "
                "be checked; the persona is kept", ai_id, exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    f"The persona '{ai_id}' was created, but the import failed ({exc}) and "
                    "whether its memories were imported could not be checked, so the persona "
                    "was kept. To try again, delete this persona and import the pack as a new "
                    "persona. Importing the same pack with 'merge' into this persona adds the "
                    "memories that are missing, but not the tasks, schedules, items or private room."
                ),
            )
        problems = _discard_created_persona(manager, ai_id, room_id)
        if problems:
            LOGGER.error(
                "Persona pack import into the new persona %s failed and it could not be fully "
                "removed: %s", ai_id, problems, exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    f"The import failed ({exc}), and removing what was created for it did not "
                    f"finish: {'; '.join(problems)}. Delete the leftovers before importing the "
                    "pack as a new persona again."
                ),
            )
        LOGGER.warning(
            "Persona pack import into the new persona %s failed; removed it and its private room %s",
            ai_id, room_id, exc_info=True,
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
    """パックから新しいペルソナを迎える (intent §5 の新規作成)。結果の内訳を返す。

    同じパック (manifest の persona_id で同一視。無ければファイルの sha256) の
    取り込みが走っている間に届いた二つ目は 409 で断る — 名前の重複検査は作成の
    前に行うので、並列の POST が両方とも検査を通って同名のペルソナを二体作るのを防ぐ。
    """
    zip_path = await _save_upload(file)
    try:
        manifest = _read_manifest(zip_path)
        source_id = manifest.get("persona_id")
        if isinstance(source_id, str) and source_id:
            claim_key = f"import-new:persona:{source_id}"
        else:
            claim_key = f"import-new:sha256:{await run_in_threadpool(_file_sha256, zip_path)}"
        with _claim_import(
            claim_key, detail="This persona pack is already being imported as a new persona.",
        ):
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
