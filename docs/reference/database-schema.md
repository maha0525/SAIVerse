<!-- 🤖 AUTO-GENERATED — 手で編集しない。次回生成で上書きされる。 -->
<!-- 源: database.models.Base.metadata / 再生成: python scripts/gen_reference_docs.py -->

# データベーススキーマ

SAIVerse の全テーブル・カラム定義（自動生成）。SQLite。本番 DB は
`~/.saiverse/user_data/database/saiverse.db`。概念的な位置づけは
[concepts/](../concepts/README.md) 各ページを参照。

**テーブル数**: 43

## addon_config

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `addon_name` | VARCHAR(255) | PK, NOT NULL |  |
| `is_enabled` | BOOLEAN | NOT NULL, default=True |  |
| `params_json` | TEXT | — |  |
| `updated_at` | DATETIME | NOT NULL |  |

## addon_message_metadata

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | INTEGER | PK, NOT NULL |  |
| `message_id` | VARCHAR(255) | NOT NULL |  |
| `addon_name` | VARCHAR(100) | NOT NULL |  |
| `key` | VARCHAR(100) | NOT NULL |  |
| `value` | TEXT | — |  |
| `created_at` | DATETIME | NOT NULL |  |

## building

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `CITYID` | INTEGER | FK→city.CITYID, NOT NULL |  |
| `BUILDINGID` | VARCHAR(255) | PK, NOT NULL |  |
| `BUILDINGNAME` | VARCHAR(32) | NOT NULL |  |
| `CAPACITY` | INTEGER | NOT NULL, default=1 |  |
| `SYSTEM_INSTRUCTION` | VARCHAR(4096) | NOT NULL, default='' |  |
| `ENTRY_PROMPT` | VARCHAR(4096) | NOT NULL, default='' |  |
| `AUTO_PROMPT` | VARCHAR(4096) | NOT NULL, default='' |  |
| `DESCRIPTION` | VARCHAR(1024) | NOT NULL, default='' |  |
| `AUTO_INTERVAL_SEC` | INTEGER | NOT NULL, default=10 |  |
| `IMAGE_PATH` | VARCHAR(512) | — |  |
| `EXTRA_PROMPT_FILES` | TEXT | — |  |
| `MAP_X` | FLOAT | — |  |
| `MAP_Y` | FLOAT | — |  |
| `PHYSICAL_VESSEL_ID` | VARCHAR(64) | — |  |
| `REGION_ID` | VARCHAR(255) | FK→region.REGION_ID |  |

## city

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `USERID` | INTEGER | FK→user.USERID, NOT NULL |  |
| `CITYID` | INTEGER | PK, NOT NULL |  |
| `CITYNAME` | VARCHAR(32) | NOT NULL |  |
| `DESCRIPTION` | VARCHAR(1024) | NOT NULL, default='' |  |
| `TIMEZONE` | VARCHAR(64) | NOT NULL, default='UTC' |  |
| `UI_PORT` | INTEGER | NOT NULL |  |
| `API_PORT` | INTEGER | NOT NULL |  |
| `START_IN_ONLINE_MODE` | BOOLEAN | NOT NULL, default=False |  |
| `HOST_AVATAR_IMAGE` | VARCHAR(255) | — |  |
| `MAP_BACKGROUND_IMAGE` | VARCHAR(512) | — |  |
| `LAST_KNOWN_VERSION` | VARCHAR(64) | — |  |

## item

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `ITEM_ID` | VARCHAR(36) | PK, NOT NULL |  |
| `NAME` | VARCHAR(255) | NOT NULL |  |
| `TYPE` | VARCHAR(64) | NOT NULL, default='object' |  |
| `DESCRIPTION` | VARCHAR(2048) | NOT NULL, default='' |  |
| `FILE_PATH` | VARCHAR(512) | — |  |
| `STATE_JSON` | VARCHAR | — |  |
| `CREATOR_ID` | VARCHAR(255) | — |  |
| `SOURCE_CONTEXT` | VARCHAR | — |  |
| `CREATED_AT` | DATETIME | NOT NULL |  |
| `UPDATED_AT` | DATETIME | NOT NULL |  |

## llm_usage_log

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `ID` | INTEGER | PK, NOT NULL |  |
| `TIMESTAMP` | DATETIME | NOT NULL |  |
| `PERSONA_ID` | VARCHAR(255) | — |  |
| `BUILDING_ID` | VARCHAR(255) | — |  |
| `MODEL_ID` | VARCHAR(255) | NOT NULL |  |
| `INPUT_TOKENS` | INTEGER | NOT NULL |  |
| `OUTPUT_TOKENS` | INTEGER | NOT NULL |  |
| `CACHED_TOKENS` | INTEGER | default=0 |  |
| `COST_USD` | FLOAT | — |  |
| `CURRENCY` | VARCHAR(8) | default='USD' |  |
| `NODE_TYPE` | VARCHAR(64) | — |  |
| `PLAYBOOK_NAME` | VARCHAR(255) | — |  |
| `CATEGORY` | VARCHAR(64) | — |  |

## phenomenon_rule

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `RULE_ID` | INTEGER | PK, NOT NULL |  |
| `TRIGGER_TYPE` | VARCHAR(64) | NOT NULL |  |
| `CONDITION_JSON` | TEXT | — |  |
| `PHENOMENON_NAME` | VARCHAR(255) | NOT NULL |  |
| `ARGUMENT_MAPPING_JSON` | TEXT | — |  |
| `ENABLED` | BOOLEAN | NOT NULL, default=True |  |
| `PRIORITY` | INTEGER | NOT NULL, default=0 |  |
| `DESCRIPTION` | VARCHAR(1024) | NOT NULL, default='' |  |
| `CREATED_AT` | DATETIME | NOT NULL |  |
| `UPDATED_AT` | DATETIME | NOT NULL |  |

## realtime_spell_binding

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `BINDING_ID` | INTEGER | PK, NOT NULL |  |
| `OWNER_KIND` | VARCHAR(32) | NOT NULL |  |
| `OWNER_ID` | VARCHAR(255) | NOT NULL |  |
| `SPELL_NAME` | VARCHAR(255) | NOT NULL |  |
| `SPELL_ARGS_JSON` | TEXT | — |  |
| `LABEL` | VARCHAR(255) | — |  |
| `ENABLED` | BOOLEAN | NOT NULL, default=True |  |
| `PRIORITY` | INTEGER | NOT NULL, default=0 |  |
| `CREATED_AT` | DATETIME | NOT NULL |  |
| `UPDATED_AT` | DATETIME | NOT NULL |  |

## region

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `REGION_ID` | VARCHAR(255) | PK, NOT NULL |  |
| `CITYID` | INTEGER | FK→city.CITYID, NOT NULL |  |
| `PARENT_REGION_ID` | VARCHAR(255) | FK→region.REGION_ID |  |
| `NAME` | VARCHAR(64) | NOT NULL |  |
| `DESCRIPTION` | VARCHAR(2048) | NOT NULL, default='' |  |
| `REGION_TYPE` | VARCHAR(32) | NOT NULL, default='generic' |  |
| `RULER_ID` | VARCHAR(255) | — |  |
| `ENTRANCE_BUILDING_ID` | VARCHAR(255) | — |  |
| `MAP_BACKGROUND_IMAGE` | VARCHAR(1024) | — |  |
| `STATE_JSON` | TEXT | — |  |
| `CONFIG_JSON` | TEXT | — |  |
| `CREATED_AT` | DATETIME | NOT NULL |  |
| `UPDATED_AT` | DATETIME | NOT NULL |  |

## tool

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `TOOLID` | INTEGER | PK, NOT NULL |  |
| `TOOLNAME` | VARCHAR(32) | NOT NULL |  |
| `MODULE_PATH` | VARCHAR(255) | NOT NULL |  |
| `FUNCTION_NAME` | VARCHAR(255) | NOT NULL, default='' |  |
| `DESCRIPTION` | VARCHAR(1024) | NOT NULL, default='' |  |

## user

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `USERID` | INTEGER | PK, NOT NULL |  |
| `PASSWORD` | VARCHAR(32) | NOT NULL |  |
| `USERNAME` | VARCHAR(32) | NOT NULL |  |
| `MAILADDRESS` | VARCHAR(64) | — |  |
| `LOGGED_IN` | BOOLEAN | NOT NULL, default=False |  |
| `CURRENT_CITYID` | INTEGER | FK→city.CITYID |  |
| `CURRENT_BUILDINGID` | VARCHAR(255) | FK→building.BUILDINGID |  |
| `AVATAR_IMAGE` | VARCHAR(255) | — |  |

## ai

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `AIID` | VARCHAR(255) | PK, NOT NULL |  |
| `HOME_CITYID` | INTEGER | FK→city.CITYID, NOT NULL |  |
| `AINAME` | VARCHAR(32) | NOT NULL |  |
| `SYSTEMPROMPT` | VARCHAR(4096) | NOT NULL, default='' |  |
| `DESCRIPTION` | VARCHAR(1024) | NOT NULL, default='' |  |
| `AVATAR_IMAGE` | VARCHAR(255) | — |  |
| `APPEARANCE_IMAGE_PATH` | VARCHAR(512) | — |  |
| `EMOTION` | VARCHAR(1024) | — |  |
| `AUTO_COUNT` | INTEGER | NOT NULL, default=0 |  |
| `LAST_AUTO_PROMPT_TIMES` | VARCHAR(2048) | — |  |
| `IS_DISPATCHED` | BOOLEAN | NOT NULL, default=False |  |
| `DEFAULT_MODEL` | VARCHAR(255) | — |  |
| `LIGHTWEIGHT_MODEL` | VARCHAR(255) | — |  |
| `LIGHTWEIGHT_VISION_MODEL` | VARCHAR(255) | — |  |
| `VISION_MODEL` | VARCHAR(255) | — |  |
| `AUDIO_MODEL` | VARCHAR(255) | — |  |
| `VIDEO_MODEL` | VARCHAR(255) | — |  |
| `MEMORY_WEAVE_MODEL` | VARCHAR(255) | — |  |
| `PRIVATE_ROOM_ID` | VARCHAR(255) | FK→building.BUILDINGID |  |
| `CHRONICLE_ENABLED` | BOOLEAN | NOT NULL, default=True |  |
| `AUTONOMOUS_CHRONICLE_ENABLED` | BOOLEAN | NOT NULL, default=True |  |
| `AUTO_RECALL_ENABLED` | BOOLEAN | NOT NULL, default=True |  |
| `MEMORY_WEAVE_CONTEXT` | BOOLEAN | NOT NULL, default=True |  |
| `MEMOPEDIA_INDEX_LIMIT` | INTEGER | — |  |
| `SPELL_ENABLED` | BOOLEAN | NOT NULL, default=True |  |
| `REALTIME_INFO_ENABLED` | BOOLEAN | NOT NULL, default=True |  |
| `METABOLISM_ANCHORS` | TEXT | — |  |
| `ACTIVITY_STATE` | VARCHAR(32) | NOT NULL, default='Idle' |  |
| `SLEEP_ON_CACHE_EXPIRE` | BOOLEAN | NOT NULL, default=True |  |
| `LAST_KNOWN_VERSION` | VARCHAR(64) | — |  |
| `META_JUDGMENT_CONFIG` | TEXT | — |  |
| `PERSONA_ROLE` | VARCHAR(32) | — |  |
| `USER_CONV_TIMEOUT_MINUTES` | INTEGER | — |  |
| `LIFE_PURPOSE` | TEXT | — |  |

## blueprint

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `BLUEPRINT_ID` | INTEGER | PK, NOT NULL |  |
| `CITYID` | INTEGER | FK→city.CITYID, NOT NULL |  |
| `NAME` | VARCHAR(255) | NOT NULL |  |
| `ENTITY_TYPE` | VARCHAR(50) | NOT NULL, default='ai' |  |
| `DESCRIPTION` | VARCHAR(1024) | NOT NULL, default='' |  |
| `BASE_SYSTEM_PROMPT` | VARCHAR(4096) | NOT NULL, default='' |  |
| `BASE_AVATAR` | VARCHAR(255) | — |  |

## building_tool_link

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `BUILDINGID` | VARCHAR(255) | PK, FK→building.BUILDINGID, NOT NULL |  |
| `TOOLID` | INTEGER | PK, FK→tool.TOOLID, NOT NULL |  |

## fixture

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `FIXTURE_ID` | VARCHAR(36) | PK, NOT NULL |  |
| `BUILDING_ID` | VARCHAR(255) | FK→building.BUILDINGID, NOT NULL |  |
| `NAME` | VARCHAR(255) | NOT NULL |  |
| `TYPE` | VARCHAR(64) | NOT NULL, default='object' |  |
| `DESCRIPTION` | VARCHAR(2048) | NOT NULL, default='' |  |
| `STATE_JSON` | TEXT | — |  |
| `FILE_PATH` | VARCHAR(512) | — |  |
| `CREATOR_ID` | VARCHAR(255) | — |  |
| `SOURCE_CONTEXT` | VARCHAR | — |  |
| `CREATED_AT` | DATETIME | NOT NULL |  |
| `UPDATED_AT` | DATETIME | NOT NULL |  |

## item_location

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `LOCATION_ID` | INTEGER | PK, NOT NULL |  |
| `ITEM_ID` | VARCHAR(36) | FK→item.ITEM_ID, NOT NULL |  |
| `OWNER_KIND` | VARCHAR(32) | NOT NULL |  |
| `OWNER_ID` | VARCHAR(255) | NOT NULL |  |
| `SLOT_NUMBER` | INTEGER | — |  |
| `UPDATED_AT` | DATETIME | NOT NULL |  |

## playbook_permission

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | INTEGER | PK, NOT NULL |  |
| `CITYID` | INTEGER | FK→city.CITYID, NOT NULL |  |
| `playbook_name` | VARCHAR(255) | NOT NULL |  |
| `permission_level` | VARCHAR(32) | NOT NULL, default='ask_every_time' |  |
| `updated_at` | DATETIME | — |  |

## user_settings

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `USERID` | INTEGER | PK, FK→user.USERID, NOT NULL |  |
| `TUTORIAL_COMPLETED` | BOOLEAN | NOT NULL, default=False |  |
| `TUTORIAL_COMPLETED_AT` | DATETIME | — |  |
| `LAST_TUTORIAL_VERSION` | INTEGER | NOT NULL, default=1 |  |
| `SELECTED_META_PLAYBOOK` | VARCHAR(255) | — |  |
| `FAVORITE_MODELS` | TEXT | — |  |

## visiting_ai

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | INTEGER | PK, NOT NULL |  |
| `city_id` | INTEGER | FK→city.CITYID, NOT NULL |  |
| `persona_id` | VARCHAR(255) | NOT NULL |  |
| `profile_json` | VARCHAR | NOT NULL |  |
| `status` | VARCHAR(32) | NOT NULL, default='requested' |  |
| `reason` | VARCHAR(255) | — |  |
| `created_at` | DATETIME | NOT NULL |  |

## action_track

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `track_id` | VARCHAR(36) | PK, NOT NULL |  |
| `persona_id` | VARCHAR(255) | FK→ai.AIID, NOT NULL |  |
| `short_id` | INTEGER | — |  |
| `title` | VARCHAR(255) | — |  |
| `track_type` | VARCHAR(64) | NOT NULL |  |
| `is_persistent` | BOOLEAN | NOT NULL, default=False |  |
| `output_target` | VARCHAR(255) | NOT NULL, default='none' |  |
| `status` | VARCHAR(32) | NOT NULL, default='unstarted' |  |
| `is_forgotten` | BOOLEAN | NOT NULL, default=False |  |
| `intent` | TEXT | — |  |
| `track_metadata` | TEXT | — |  |
| `last_active_at` | DATETIME | — |  |
| `created_at` | DATETIME | NOT NULL |  |
| `completed_at` | DATETIME | — |  |
| `aborted_at` | DATETIME | — |  |

## addon_persona_config

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | INTEGER | PK, NOT NULL |  |
| `addon_name` | VARCHAR(255) | NOT NULL |  |
| `persona_id` | VARCHAR(255) | FK→ai.AIID, NOT NULL |  |
| `params_json` | TEXT | NOT NULL |  |
| `updated_at` | DATETIME | NOT NULL |  |

## ai_tool_link

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `AIID` | VARCHAR(255) | PK, FK→ai.AIID, NOT NULL |  |
| `TOOLID` | INTEGER | PK, FK→tool.TOOLID, NOT NULL |  |

## building_messages

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | INTEGER | PK, NOT NULL |  |
| `building_id` | VARCHAR(255) | FK→building.BUILDINGID, NOT NULL |  |
| `seq` | INTEGER | NOT NULL |  |
| `role` | VARCHAR(32) | NOT NULL |  |
| `persona_id` | VARCHAR(255) | FK→ai.AIID |  |
| `content` | TEXT | NOT NULL |  |
| `timestamp` | VARCHAR(64) | NOT NULL |  |
| `heard_by` | TEXT | NOT NULL, default='[]' |  |
| `ingested_by` | TEXT | NOT NULL, default='[]' |  |
| `event_type` | VARCHAR(32) | — |  |
| `event_data` | TEXT | — |  |
| `metadata_json` | TEXT | — |  |
| `message_id` | VARCHAR(255) | — |  |
| `client_message_id` | VARCHAR(64) | — |  |
| `origin_track_id` | VARCHAR(36) | — |  |
| `pulse_id` | VARCHAR(36) | — |  |
| `legacy_seq` | INTEGER | — |  |
| `legacy_message_id` | VARCHAR(255) | — |  |
| `created_at` | DATETIME | NOT NULL |  |

## building_occupancy_log

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `ID` | INTEGER | PK, NOT NULL |  |
| `CITYID` | INTEGER | FK→city.CITYID, NOT NULL |  |
| `BUILDINGID` | VARCHAR(255) | FK→building.BUILDINGID, NOT NULL |  |
| `AIID` | VARCHAR(255) | FK→ai.AIID, NOT NULL |  |
| `ENTRY_TIMESTAMP` | DATETIME | NOT NULL |  |
| `EXIT_TIMESTAMP` | DATETIME | — |  |

## line_head_snapshot

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `PERSONA_ID` | VARCHAR(255) | PK, FK→ai.AIID, NOT NULL |  |
| `LINE_ID` | VARCHAR(255) | PK, NOT NULL |  |
| `LINE_ROLE` | VARCHAR(32) | NOT NULL |  |
| `MODEL_KEY` | VARCHAR(128) | NOT NULL |  |
| `SECTIONS_JSON` | TEXT | NOT NULL |  |
| `LAST_NOTIFIED_JSON` | TEXT | NOT NULL |  |
| `SNAPSHOT_VERSION` | INTEGER | NOT NULL, default=1 |  |
| `CAPTURED_AT` | DATETIME | NOT NULL |  |
| `UPDATED_AT` | DATETIME | NOT NULL |  |

## meta_judgment_log

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `judgment_id` | VARCHAR(36) | PK, NOT NULL |  |
| `persona_id` | VARCHAR(255) | FK→ai.AIID, NOT NULL |  |
| `judged_at` | DATETIME | NOT NULL |  |
| `track_at_judgment_id` | VARCHAR(36) | — |  |
| `trigger_type` | VARCHAR(32) | NOT NULL |  |
| `trigger_context` | TEXT | — |  |
| `prompt_snapshot` | TEXT | — |  |
| `judgment_thought` | TEXT | — |  |
| `spells_emitted` | TEXT | — |  |
| `committed_to_main_cache` | BOOLEAN | NOT NULL, default=False |  |

## note

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `note_id` | VARCHAR(36) | PK, NOT NULL |  |
| `persona_id` | VARCHAR(255) | FK→ai.AIID, NOT NULL |  |
| `title` | VARCHAR(255) | NOT NULL |  |
| `note_type` | VARCHAR(32) | NOT NULL |  |
| `description` | TEXT | — |  |
| `note_metadata` | TEXT | — |  |
| `is_active` | BOOLEAN | NOT NULL, default=True |  |
| `created_at` | DATETIME | NOT NULL |  |
| `last_opened_at` | DATETIME | — |  |
| `closed_at` | DATETIME | — |  |

## observer_config

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `OBSERVER_ID` | VARCHAR(36) | PK, NOT NULL |  |
| `FIXTURE_ID` | VARCHAR(36) | FK→fixture.FIXTURE_ID, NOT NULL |  |
| `ENABLED` | BOOLEAN | NOT NULL, default=True |  |
| `EXEC_KIND` | VARCHAR(32) | NOT NULL |  |
| `EXEC_TARGET` | VARCHAR(255) | — |  |
| `EXEC_ARGS_JSON` | TEXT | — |  |
| `INTERVAL_SEC` | INTEGER | — |  |
| `METRIC_KEYS_JSON` | TEXT | — |  |
| `NOTIFY_RULES_JSON` | TEXT | — |  |
| `CREATED_AT` | DATETIME | NOT NULL |  |
| `UPDATED_AT` | DATETIME | NOT NULL |  |

## persona_building_state

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `PERSONA_ID` | VARCHAR(255) | PK, FK→ai.AIID, NOT NULL |  |
| `BUILDING_ID` | VARCHAR(255) | PK, FK→building.BUILDINGID, NOT NULL |  |
| `BASELINE_JSON` | TEXT | — |  |
| `LAST_NOTIFIED_JSON` | TEXT | — |  |
| `UPDATED_AT` | DATETIME | NOT NULL |  |

## persona_event_log

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `EVENT_ID` | INTEGER | PK, NOT NULL |  |
| `PERSONA_ID` | VARCHAR(255) | FK→ai.AIID, NOT NULL |  |
| `CREATED_AT` | DATETIME | NOT NULL |  |
| `CONTENT` | VARCHAR | NOT NULL |  |
| `STATUS` | VARCHAR(32) | NOT NULL, default='pending' |  |
| `EVENT_TYPE` | VARCHAR(64) | — |  |
| `PAYLOAD` | TEXT | — |  |

## persona_pulse_cursor

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `PERSONA_ID` | VARCHAR(255) | PK, FK→ai.AIID, NOT NULL |  |
| `BUILDING_ID` | VARCHAR(255) | PK, FK→building.BUILDINGID, NOT NULL |  |
| `CURSOR_SEQ` | INTEGER | NOT NULL, default=0 |  |
| `ENTRY_MARKER_SEQ` | INTEGER | NOT NULL, default=0 |  |
| `UPDATED_AT` | DATETIME | NOT NULL |  |

## persona_schedule

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `SCHEDULE_ID` | INTEGER | PK, NOT NULL |  |
| `PERSONA_ID` | VARCHAR(255) | FK→ai.AIID, NOT NULL |  |
| `SCHEDULE_TYPE` | VARCHAR(32) | NOT NULL |  |
| `META_PLAYBOOK` | VARCHAR(255) | NOT NULL |  |
| `ENABLED` | BOOLEAN | NOT NULL, default=True |  |
| `DESCRIPTION` | VARCHAR(512) | NOT NULL, default='' |  |
| `PRIORITY` | INTEGER | NOT NULL, default=0 |  |
| `DAYS_OF_WEEK` | VARCHAR(255) | — |  |
| `TIME_OF_DAY` | VARCHAR(8) | — |  |
| `SCHEDULED_DATETIME` | DATETIME | — |  |
| `COMPLETED` | BOOLEAN | NOT NULL, default=False |  |
| `INTERVAL_SECONDS` | INTEGER | — |  |
| `LAST_EXECUTED_AT` | DATETIME | — |  |
| `PLAYBOOK_PARAMS` | TEXT | — |  |
| `CREATED_AT` | DATETIME | NOT NULL |  |
| `UPDATED_AT` | DATETIME | NOT NULL |  |

## playbooks

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | INTEGER | PK, NOT NULL |  |
| `name` | VARCHAR(255) | NOT NULL |  |
| `display_name` | VARCHAR(255) | — |  |
| `description` | VARCHAR(1024) | NOT NULL, default='' |  |
| `scope` | VARCHAR(32) | NOT NULL, default='public' |  |
| `created_by_persona_id` | VARCHAR(255) | FK→ai.AIID |  |
| `building_id` | VARCHAR(255) | FK→building.BUILDINGID |  |
| `schema_json` | TEXT | NOT NULL |  |
| `nodes_json` | TEXT | NOT NULL |  |
| `router_callable` | BOOLEAN | NOT NULL, default=False |  |
| `user_selectable` | BOOLEAN | NOT NULL, default=False |  |
| `dev_only` | BOOLEAN | NOT NULL, default=False |  |
| `required_credentials` | TEXT | — |  |
| `source_file` | VARCHAR(512) | — |  |
| `source_hash` | VARCHAR(64) | — |  |
| `created_at` | DATETIME | NOT NULL |  |
| `updated_at` | DATETIME | NOT NULL |  |

## thinking_request

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | INTEGER | PK, NOT NULL |  |
| `request_id` | VARCHAR(36) | NOT NULL |  |
| `city_id` | INTEGER | FK→city.CITYID, NOT NULL |  |
| `persona_id` | VARCHAR(255) | FK→ai.AIID, NOT NULL |  |
| `request_context_json` | VARCHAR | NOT NULL |  |
| `response_text` | VARCHAR | — |  |
| `status` | VARCHAR(32) | NOT NULL, default='pending' |  |
| `created_at` | DATETIME | NOT NULL |  |

## user_ai_link

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `USERID` | INTEGER | PK, FK→user.USERID, NOT NULL |  |
| `AIID` | VARCHAR(255) | PK, FK→ai.AIID, NOT NULL |  |

## note_message

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `note_id` | VARCHAR(36) | PK, FK→note.note_id, NOT NULL |  |
| `message_id` | VARCHAR(255) | PK, NOT NULL |  |
| `added_at` | DATETIME | NOT NULL |  |
| `auto_added` | BOOLEAN | NOT NULL, default=False |  |

## note_page

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `note_id` | VARCHAR(36) | PK, FK→note.note_id, NOT NULL |  |
| `page_id` | VARCHAR(255) | PK, NOT NULL |  |

## observer_metrics

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | INTEGER | PK, NOT NULL |  |
| `OBSERVER_ID` | VARCHAR(36) | FK→observer_config.OBSERVER_ID, NOT NULL |  |
| `METRIC_NAME` | VARCHAR(64) | NOT NULL |  |
| `VALUE_NUM` | FLOAT | — |  |
| `VALUE_TEXT` | TEXT | — |  |
| `RECORDED_AT` | DATETIME | NOT NULL |  |

## persona_task

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | VARCHAR(36) | PK, NOT NULL |  |
| `persona_id` | VARCHAR(255) | FK→ai.AIID, NOT NULL |  |
| `short_id` | INTEGER | — |  |
| `parent_kind` | VARCHAR(16) | — |  |
| `note_id` | VARCHAR(36) | FK→note.note_id |  |
| `track_id` | VARCHAR(36) | FK→action_track.track_id |  |
| `title` | VARCHAR(255) | NOT NULL |  |
| `goal` | TEXT | NOT NULL, default='' |  |
| `summary` | TEXT | NOT NULL, default='' |  |
| `notes` | TEXT | — |  |
| `status` | VARCHAR(32) | NOT NULL, default='pending' |  |
| `priority` | VARCHAR(16) | NOT NULL, default='normal' |  |
| `origin` | VARCHAR(32) | NOT NULL, default='auto' |  |
| `active_step_id` | VARCHAR(36) | — |  |
| `due_at` | DATETIME | — |  |
| `created_at` | DATETIME | NOT NULL |  |
| `updated_at` | DATETIME | NOT NULL |  |
| `completed_at` | DATETIME | — |  |
| `version` | INTEGER | NOT NULL, default=0 |  |
| `last_actor` | VARCHAR(255) | — |  |

## track_local_log

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `log_id` | VARCHAR(36) | PK, NOT NULL |  |
| `track_id` | VARCHAR(36) | FK→action_track.track_id, NOT NULL |  |
| `occurred_at` | DATETIME | NOT NULL |  |
| `log_kind` | VARCHAR(64) | NOT NULL |  |
| `payload` | TEXT | — |  |
| `source_line_id` | VARCHAR(36) | — |  |
| `visible_to_other_tracks` | BOOLEAN | NOT NULL, default=False |  |

## track_open_note

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `track_id` | VARCHAR(36) | PK, FK→action_track.track_id, NOT NULL |  |
| `note_id` | VARCHAR(36) | PK, FK→note.note_id, NOT NULL |  |
| `opened_at` | DATETIME | NOT NULL |  |

## persona_task_step

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | VARCHAR(36) | PK, NOT NULL |  |
| `task_id` | VARCHAR(36) | FK→persona_task.id, NOT NULL |  |
| `position` | INTEGER | NOT NULL |  |
| `title` | VARCHAR(255) | NOT NULL |  |
| `description` | TEXT | — |  |
| `status` | VARCHAR(32) | NOT NULL, default='pending' |  |
| `notes` | TEXT | — |  |
| `created_at` | DATETIME | NOT NULL |  |
| `updated_at` | DATETIME | NOT NULL |  |
| `completed_at` | DATETIME | — |  |
| `version` | INTEGER | NOT NULL, default=0 |  |

## persona_task_history

| カラム | 型 | 制約 | 説明 |
|---|---|---|---|
| `id` | VARCHAR(36) | PK, NOT NULL |  |
| `task_id` | VARCHAR(36) | FK→persona_task.id, NOT NULL |  |
| `step_id` | VARCHAR(36) | FK→persona_task_step.id |  |
| `event_type` | VARCHAR(64) | NOT NULL |  |
| `payload` | TEXT | — |  |
| `actor` | VARCHAR(255) | — |  |
| `created_at` | DATETIME | NOT NULL |  |
