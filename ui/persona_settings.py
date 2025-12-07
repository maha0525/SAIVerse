from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import gradio as gr
import pandas as pd
from gradio.events import SelectData

from database.models import PersonaSchedule, Playbook as PlaybookModel, AI as AIModel, City as CityModel

LOGGER = logging.getLogger(__name__)


def _get_persona_timezone(manager, persona_id: str) -> ZoneInfo:
    """ペルソナのホームCityのタイムゾーンを取得"""
    session = manager.SessionLocal()
    try:
        persona_model = session.query(AIModel).filter(AIModel.AIID == persona_id).first()
        if not persona_model:
            LOGGER.warning("Persona %s not found in database", persona_id)
            return ZoneInfo("UTC")

        city_model = session.query(CityModel).filter(CityModel.CITYID == persona_model.HOME_CITYID).first()
        if not city_model or not city_model.TIMEZONE:
            LOGGER.warning("City timezone not found for persona %s", persona_id)
            return ZoneInfo("UTC")

        return ZoneInfo(city_model.TIMEZONE)
    except Exception as e:
        LOGGER.warning("Failed to get timezone for persona %s: %s", persona_id, e)
        return ZoneInfo("UTC")
    finally:
        session.close()


def _persona_choices(manager) -> List[tuple[str, str]]:
    """ペルソナの選択肢リストを生成"""
    choices: List[tuple[str, str]] = []
    if not manager:
        return choices
    for persona_id, persona in manager.personas.items():
        display = persona.persona_name or persona_id
        choices.append((display, persona_id))
    choices.sort(key=lambda item: item[0])
    return choices


def _get_user_selectable_playbooks(manager) -> List[tuple[str, str]]:
    """user_selectable=Trueのメタプレイブックを取得"""
    session = manager.SessionLocal()
    try:
        playbooks = (
            session.query(PlaybookModel)
            .filter(
                PlaybookModel.user_selectable == True,
                PlaybookModel.name.like("meta_%"),
            )
            .all()
        )
        choices = [(pb.name, pb.name) for pb in playbooks]
        choices.sort(key=lambda item: item[0])
        return choices
    except Exception as e:
        LOGGER.error("Failed to fetch user_selectable playbooks: %s", e, exc_info=True)
        return []
    finally:
        session.close()


def _empty_schedule_table() -> pd.DataFrame:
    """空のスケジュールテーブルを返す"""
    return pd.DataFrame(
        columns=[
            "ID",
            "タイプ",
            "プレイブック",
            "説明",
            "優先度",
            "有効",
            "設定詳細",
        ]
    )


def _load_schedules(manager, persona_id: str) -> tuple[pd.DataFrame, str]:
    """ペルソナのスケジュール一覧を読み込む"""
    if not persona_id:
        return _empty_schedule_table(), "ペルソナを選んでね。"

    session = manager.SessionLocal()
    try:
        schedules = (
            session.query(PersonaSchedule)
            .filter(PersonaSchedule.PERSONA_ID == persona_id)
            .order_by(PersonaSchedule.PRIORITY.desc(), PersonaSchedule.SCHEDULE_ID.desc())
            .all()
        )

        if not schedules:
            return _empty_schedule_table(), "スケジュールがまだないよ。"

        # ペルソナのタイムゾーンを取得（表示用）
        persona_tz = _get_persona_timezone(manager, persona_id)

        rows = []
        for s in schedules:
            schedule_type = s.SCHEDULE_TYPE
            detail = ""

            if schedule_type == "periodic":
                days = "毎日"
                if s.DAYS_OF_WEEK:
                    try:
                        day_list = json.loads(s.DAYS_OF_WEEK)
                        day_names = ["月", "火", "水", "木", "金", "土", "日"]
                        days = ", ".join([day_names[d] for d in day_list if 0 <= d < 7])
                    except Exception:
                        pass
                detail = f"{days} {s.TIME_OF_DAY or '??:??'}"

            elif schedule_type == "oneshot":
                if s.SCHEDULED_DATETIME:
                    # UTCからペルソナのタイムゾーンに変換して表示
                    dt_utc = s.SCHEDULED_DATETIME
                    if dt_utc.tzinfo is None:
                        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                    dt_local = dt_utc.astimezone(persona_tz)
                    detail = dt_local.strftime("%Y-%m-%d %H:%M")
                else:
                    detail = "未設定"
                if s.COMPLETED:
                    detail += " (完了)"

            elif schedule_type == "interval":
                interval_sec = s.INTERVAL_SECONDS or 0
                detail = f"間隔: {interval_sec}秒"
                if s.LAST_EXECUTED_AT:
                    detail += f" (最終実行: {s.LAST_EXECUTED_AT.strftime('%Y-%m-%d %H:%M')})"

            rows.append(
                [
                    s.SCHEDULE_ID,
                    schedule_type,
                    s.META_PLAYBOOK,
                    s.DESCRIPTION or "",
                    s.PRIORITY,
                    "✓" if s.ENABLED else "",
                    detail,
                ]
            )

        df = pd.DataFrame(
            rows,
            columns=["ID", "タイプ", "プレイブック", "説明", "優先度", "有効", "設定詳細"],
        )
        return df, f"{len(schedules)}件のスケジュールを取得したよ。"

    except Exception as e:
        LOGGER.error("Failed to load schedules: %s", e, exc_info=True)
        return _empty_schedule_table(), f"エラー: {e}"
    finally:
        session.close()


def _add_schedule(
    manager,
    persona_id: str,
    schedule_type: str,
    meta_playbook: str,
    description: str,
    priority: int,
    enabled: bool,
    # periodic
    days_of_week: List[str],
    time_of_day: str,
    # oneshot
    scheduled_datetime_str: str,
    # interval
    interval_seconds: int,
) -> tuple[pd.DataFrame, str]:
    """スケジュールを追加"""
    if not persona_id:
        return _load_schedules(manager, persona_id)[0], "ペルソナを選んでね。"

    if not meta_playbook:
        return _load_schedules(manager, persona_id)[0], "メタプレイブックを選んでね。"

    session = manager.SessionLocal()
    try:
        new_schedule = PersonaSchedule(
            PERSONA_ID=persona_id,
            SCHEDULE_TYPE=schedule_type,
            META_PLAYBOOK=meta_playbook,
            DESCRIPTION=description,
            PRIORITY=priority,
            ENABLED=enabled,
        )

        if schedule_type == "periodic":
            # 曜日をJSONに変換
            weekday_map = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}
            day_indices = [weekday_map[d] for d in days_of_week if d in weekday_map]
            new_schedule.DAYS_OF_WEEK = json.dumps(day_indices) if day_indices else None
            new_schedule.TIME_OF_DAY = time_of_day or None

        elif schedule_type == "oneshot":
            if scheduled_datetime_str:
                try:
                    # ペルソナのホームCityのタイムゾーンを取得
                    persona_tz = _get_persona_timezone(manager, persona_id)
                    # 入力された時刻をペルソナのタイムゾーンとして解釈
                    dt_naive = datetime.strptime(scheduled_datetime_str, "%Y-%m-%d %H:%M")
                    dt_local = dt_naive.replace(tzinfo=persona_tz)
                    # UTCに変換してDBに保存
                    dt_utc = dt_local.astimezone(timezone.utc)
                    new_schedule.SCHEDULED_DATETIME = dt_utc
                    LOGGER.info(
                        "[PersonaSettings] Oneshot schedule: input=%s, local=%s (%s), utc=%s",
                        scheduled_datetime_str,
                        dt_local.isoformat(),
                        persona_tz,
                        dt_utc.isoformat(),
                    )
                except Exception as e:
                    LOGGER.error("Failed to parse datetime: %s", e, exc_info=True)
                    return _load_schedules(manager, persona_id)[0], f"日時の形式が正しくないよ (YYYY-MM-DD HH:MM): {e}"

        elif schedule_type == "interval":
            new_schedule.INTERVAL_SECONDS = interval_seconds or None

        session.add(new_schedule)
        session.commit()

        LOGGER.info(
            "[PersonaSettings] Added schedule %d for persona %s (type=%s)",
            new_schedule.SCHEDULE_ID,
            persona_id,
            schedule_type,
        )

        return _load_schedules(manager, persona_id)[0], "スケジュールを追加したよ！"

    except Exception as e:
        LOGGER.error("Failed to add schedule: %s", e, exc_info=True)
        return _load_schedules(manager, persona_id)[0], f"エラー: {e}"
    finally:
        session.close()


def _delete_schedule(manager, persona_id: str, schedule_id: Optional[int]) -> tuple[pd.DataFrame, str]:
    """スケジュールを削除"""
    if not schedule_id:
        return _load_schedules(manager, persona_id)[0], "削除するスケジュールを選んでね。"

    session = manager.SessionLocal()
    try:
        schedule = session.query(PersonaSchedule).filter(PersonaSchedule.SCHEDULE_ID == schedule_id).first()
        if not schedule:
            return _load_schedules(manager, persona_id)[0], "スケジュールが見つからなかったよ。"

        session.delete(schedule)
        session.commit()

        LOGGER.info("[PersonaSettings] Deleted schedule %d", schedule_id)
        return _load_schedules(manager, persona_id)[0], "スケジュールを削除したよ。"

    except Exception as e:
        LOGGER.error("Failed to delete schedule: %s", e, exc_info=True)
        return _load_schedules(manager, persona_id)[0], f"エラー: {e}"
    finally:
        session.close()


def _toggle_schedule(manager, persona_id: str, schedule_id: Optional[int]) -> tuple[pd.DataFrame, str]:
    """スケジュールの有効/無効を切り替え"""
    if not schedule_id:
        return _load_schedules(manager, persona_id)[0], "切り替えるスケジュールを選んでね。"

    session = manager.SessionLocal()
    try:
        schedule = session.query(PersonaSchedule).filter(PersonaSchedule.SCHEDULE_ID == schedule_id).first()
        if not schedule:
            return _load_schedules(manager, persona_id)[0], "スケジュールが見つからなかったよ。"

        schedule.ENABLED = not schedule.ENABLED
        session.commit()

        status = "有効" if schedule.ENABLED else "無効"
        LOGGER.info("[PersonaSettings] Toggled schedule %d to %s", schedule_id, status)
        return _load_schedules(manager, persona_id)[0], f"スケジュールを{status}にしたよ。"

    except Exception as e:
        LOGGER.error("Failed to toggle schedule: %s", e, exc_info=True)
        return _load_schedules(manager, persona_id)[0], f"エラー: {e}"
    finally:
        session.close()


def _on_schedule_select(select_data: SelectData, schedule_table: pd.DataFrame) -> Optional[int]:
    """スケジュール選択時のハンドラー"""
    if not isinstance(select_data, SelectData) or select_data.index is None:
        return None

    idx = select_data.index
    if isinstance(idx, (list, tuple)):
        row_idx = idx[0]
    else:
        row_idx = idx

    if isinstance(schedule_table, pd.DataFrame) and 0 <= row_idx < len(schedule_table):
        schedule_id = schedule_table.iloc[row_idx]["ID"]
        return int(schedule_id)

    return None


def create_persona_settings_ui(manager) -> None:
    """ペルソナ設定UIを作成（メモリー設定 + スケジュール管理）"""
    # メモリー設定UIのインポート
    from tools.utilities.memory_settings_ui import create_memory_settings_ui

    gr.Markdown("## ペルソナ設定")
    gr.Markdown("ペルソナごとの長期記憶とスケジュールを管理するよ。")

    with gr.Tabs():
        # タブ1: 長期記憶管理（既存のメモリー設定）
        with gr.TabItem("長期記憶管理"):
            create_memory_settings_ui(manager)

        # タブ2: スケジュール管理（新規）
        with gr.TabItem("スケジュール管理"):
            gr.Markdown("### スケジュール管理")
            gr.Markdown(
                """
                ペルソナの定期的な行動をスケジュールできるよ。

                **スケジュールタイプ**:
                - **定期**: 特定の曜日・時刻に実行（例: 毎朝9時）
                - **単発**: 指定した日時に1回だけ実行
                - **恒常**: 一定間隔で繰り返し実行（例: 10分おき）
                """
            )

            choices = _persona_choices(manager)
            persona_ids = [pid for _, pid in choices]
            default_persona = persona_ids[0] if persona_ids else None

            persona_dropdown = gr.Dropdown(
                choices=[label for label, _ in choices] if choices else [],
                value=choices[0][0] if choices else None,
                label="ペルソナ",
                interactive=bool(choices),
            )
            persona_id_state = gr.State(default_persona)

            def _update_persona(selected_label: Optional[str], current_id: Optional[str]):
                if not choices:
                    return current_id, "ペルソナが見つからなかったよ。", _empty_schedule_table(), ""
                mapping = {label: pid for label, pid in choices}
                persona_id = mapping.get(selected_label) if selected_label else None
                if not persona_id:
                    return current_id, "そのペルソナは今使えないみたい。", _empty_schedule_table(), ""
                table, msg = _load_schedules(manager, persona_id)
                return persona_id, f"対象ペルソナ: {selected_label} ({persona_id})", table, msg

            persona_status = gr.Markdown(
                f"対象ペルソナ: {choices[0][0]} ({choices[0][1]})" if choices else "対象ペルソナがまだ無いよ。"
            )

            # スケジュール一覧
            schedule_table = gr.DataFrame(value=_empty_schedule_table(), interactive=False)
            schedule_feedback = gr.Markdown("")
            selected_schedule_id = gr.State(None)

            refresh_btn = gr.Button("スケジュール一覧を更新", variant="secondary")

            # スケジュール追加フォーム
            with gr.Accordion("スケジュールを追加", open=False):
                schedule_type_radio = gr.Radio(
                    choices=["periodic", "oneshot", "interval"],
                    value="periodic",
                    label="スケジュールタイプ",
                )

                playbook_choices = _get_user_selectable_playbooks(manager)
                meta_playbook_dropdown = gr.Dropdown(
                    choices=[label for label, _ in playbook_choices] if playbook_choices else [],
                    label="メタプレイブック",
                    interactive=bool(playbook_choices),
                )

                description_box = gr.Textbox(label="説明", placeholder="このスケジュールの説明を入力してね")
                priority_number = gr.Number(label="優先度", value=0, precision=0)
                enabled_checkbox = gr.Checkbox(label="有効にする", value=True)

                # 定期スケジュール用
                with gr.Group(visible=True) as periodic_group:
                    days_checkbox = gr.CheckboxGroup(
                        choices=["月", "火", "水", "木", "金", "土", "日"],
                        label="曜日（未選択の場合は毎日）",
                    )
                    time_box = gr.Textbox(
                        label="時刻 (HH:MM)",
                        placeholder="09:00",
                        value="09:00",
                        info="ペルソナのホームCityのタイムゾーンで入力してね",
                    )

                # 単発スケジュール用
                with gr.Group(visible=False) as oneshot_group:
                    datetime_box = gr.Textbox(
                        label="実行日時 (YYYY-MM-DD HH:MM)",
                        placeholder="2025-12-07 09:00",
                        info="ペルソナのホームCityのタイムゾーンで入力してね",
                    )

                # 恒常スケジュール用
                with gr.Group(visible=False) as interval_group:
                    interval_number = gr.Number(label="インターバル（秒）", value=600, precision=0)

                # スケジュールタイプに応じてグループの表示を切り替え
                def _update_form_visibility(schedule_type):
                    return (
                        gr.update(visible=(schedule_type == "periodic")),
                        gr.update(visible=(schedule_type == "oneshot")),
                        gr.update(visible=(schedule_type == "interval")),
                    )

                schedule_type_radio.change(
                    fn=_update_form_visibility,
                    inputs=[schedule_type_radio],
                    outputs=[periodic_group, oneshot_group, interval_group],
                )

                add_btn = gr.Button("スケジュールを追加", variant="primary")

            # スケジュール操作ボタン
            with gr.Row():
                toggle_btn = gr.Button("有効/無効を切り替え", variant="secondary")
                delete_btn = gr.Button("削除", variant="stop")

            # イベントハンドラー
            persona_dropdown.change(
                fn=_update_persona,
                inputs=[persona_dropdown, persona_id_state],
                outputs=[persona_id_state, persona_status, schedule_table, schedule_feedback],
                show_progress="hidden",
            )

            refresh_btn.click(
                fn=lambda persona_id: _load_schedules(manager, persona_id),
                inputs=[persona_id_state],
                outputs=[schedule_table, schedule_feedback],
                show_progress=True,
            )

            add_btn.click(
                fn=lambda persona_id, stype, playbook, desc, prio, enabled, days, time, dt_str, interval: _add_schedule(
                    manager,
                    persona_id,
                    stype,
                    playbook,
                    desc,
                    int(prio),
                    enabled,
                    days or [],
                    time,
                    dt_str,
                    int(interval),
                ),
                inputs=[
                    persona_id_state,
                    schedule_type_radio,
                    meta_playbook_dropdown,
                    description_box,
                    priority_number,
                    enabled_checkbox,
                    days_checkbox,
                    time_box,
                    datetime_box,
                    interval_number,
                ],
                outputs=[schedule_table, schedule_feedback],
                show_progress=True,
            )

            schedule_table.select(
                fn=_on_schedule_select,
                inputs=[schedule_table],
                outputs=[selected_schedule_id],
                show_progress="hidden",
            )

            toggle_btn.click(
                fn=lambda persona_id, schedule_id: _toggle_schedule(manager, persona_id, schedule_id),
                inputs=[persona_id_state, selected_schedule_id],
                outputs=[schedule_table, schedule_feedback],
                show_progress=True,
            )

            delete_btn.click(
                fn=lambda persona_id, schedule_id: _delete_schedule(manager, persona_id, schedule_id),
                inputs=[persona_id_state, selected_schedule_id],
                outputs=[schedule_table, schedule_feedback],
                show_progress=True,
            )
