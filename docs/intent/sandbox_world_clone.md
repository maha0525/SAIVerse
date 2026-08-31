# Intent: サンドボックス世界丸ごとコピー (`scripts/clone_world_to_test_env.py`)

- **Status**: v0.1 (2026-07-06 起草)
- **Owner**: まはー
- **関連**: `scripts/clone_persona_to_test_env.py` (ペルソナ単体複製),
  `test_fixtures/setup_test_env.py` (合成最小環境),
  `docs/intent/autonomous_behavior_v2.md` §12 (一日シミュレータ),
  メモリ `project_sandbox_persona_testing`

## 1. なぜ作るか

ペルソナ単体複製 (`clone_persona_to_test_env.py`) は「記憶は本物、世界は書き割り」
だった。合成テスト環境には Building が 2 つしかなく、本番ペルソナの写し身は:

- 私室・現在位置が Test Lobby 等へ強制再マップされる
- **persona_task / persona_day_plan (本番で育った欲求・タスク・時間割) が来ない**
  (AI 行と occupancy しか複製されないため)
- 過去の成果物 (Item) が存在しない世界で目覚める
- 他のペルソナがいない (遭遇・social 系は原理的にテスト不能)
- 施設ロールタグ (FACILITY_ROLES) が無く、手動 UPDATE 頼み

記憶が参照する場所・物・人が実在しないため、実 LLM で回すほど作話圧力がかかる。
対策は逆転の発想: **「何をコピーするか」を列挙するのをやめ、全部コピーして
「何をリセット/停止するか」だけを管理する**。スキーマが増えても自動で追従する。

## 2. 三つの複製ツールの住み分け

| ツール | 世界 | 用途 |
|---|---|---|
| `setup_test_env.py` | 合成最小 (test_city + 2 Building) | API 配線テスト・CI 的な軽量確認 |
| `clone_persona_to_test_env.py` | 既存のテスト環境に写し身 1 体を差し替え | 稼働中サンドボックスのペルソナだけ更新 |
| `clone_world_to_test_env.py` (本書) | **本番の完全な写し** | 忠実度が要る行動テスト・実 LLM 一日シム |

world clone は `setup_test_env.py` を前提にしない (dest 構造を自分で作る)。

## 3. 不変条件 (INVARIANTS)

1. **source (本番) は読み取り専用**。SQLite は `mode=ro` で開き、スナップショットは
   sqlite の backup API で取る (本番稼働中でも WAL ごと整合スナップショットになる。
   ファイル copy だと WAL 未反映分が欠ける)。
2. **dest が本番と同一パスなら拒否する** (resolve 後のパス比較)。
3. **外部への作用を持ち込まない**。複製した世界は起動しただけで外に触れてはならない:
   - `addon_config.is_enabled` を全行 0 にする (Discord / stackchan / X 等の
     アドオンが本物のデバイス・外部サービスへ繋がる事故の防止)。`--keep-addons` で
     opt-out 可 (アドオン自体をテストしたい場合)
   - `city.START_IN_ONLINE_MODE = 0` (SDS 登録の抑止)
   - `visiting_ai` / `thinking_request` を全消去 (他 City とのトランザクション残骸)
4. **ポート衝突の防止**。本番と同時起動できるよう、City のポートを
   テスト用に書き換える (既定: UI 18000 / API 18001。複数 City は +10 ずつ)。
5. **dest の既存データは確認なしに消さない** (`--force` で確認スキップ)。

## 4. 何をコピーし、何をリセットするか

### コピー (丸ごと)

- `saiverse.db` — backup API スナップショット (全テーブル: Building / Item /
  persona_task / persona_day_plan / playbooks / 施設タグ / building_messages …)
- user_data の資源ディレクトリ: `models` / `providers` / `playbooks` / `tools` /
  `prompts` / `phenomena` / `icons` (存在するもののみ)
- `--persona <id>` (複数指定可) のペルソナディレクトリ
  (`personas/<id>/` — memory.db / tasks.db 等。`.db` は backup API、他は copy2)
- home 直下の `documents/` と `image/` — Item.FILE_PATH は saiverse_home 相対
  (`manager/items.py`)。写さないと成果物アイテムが中身なしになる。`--no-media` で除外可。
  image/ は GB 級のため、メディアのみ**増分同期** (size+mtime 一致はスキップ、
  source に無い dest ファイルは削除) — 再クローンを速くする

### コピーしない

- `logs/` (本番のログはノイズ)、`backups/`
- `addon_data/` (外部サービスの状態・認証情報を含みうる。§3-3 と同根)
- 非対象ペルソナの `personas/<id>/` ディレクトリ (サイズ対策。
  DB には AI 行が残るが自律行動 OFF なので動かない)

### リセット (dest 側で実行)

| 対象 | 処置 | 理由 |
|---|---|---|
| `ai.IS_DISPATCHED` | 全行 0 | 派遣中フラグは他 City との関係 = 写しでは無効 |
| `ai.AUTO_COUNT` / `ai.LAST_AUTO_PROMPT_TIMES` | 0 / NULL | 実行時カウンタ |
| `ai.AUTONOMY_ENABLED` | **非対象ペルソナのみ `False`** | 起動時に全員が動き出すとコスト暴発 + memory.db 不在ペルソナの誤動作。対象ペルソナは本番の値を保つ。(2026-07-14 以前は `ACTIVITY_STATE='Stop'`。当時から実効は「自律が止まる」ことだけで、Stop と Idle に実装上の差は無かった — 置換後も安全機構としての意味は同一) |
| `city.UI_PORT` / `API_PORT` | テスト用ポート | 本番と同時起動 (§3-4) |
| `city.START_IN_ONLINE_MODE` | 0 | §3-3 |
| `visiting_ai` / `thinking_request` | 全消去 | §3-3 |
| `addon_config.is_enabled` | 0 (`--keep-addons` 以外) | §3-3 |

意図的に **リセットしないもの**: `building_occupancy_log` (現在位置は世界状態そのもの)、
`llm_usage_log` (履歴。害なし)、`user` (ログイン状態含め写しでよい)、
`persona_schedule` (起床・就寝は設定であって実行時状態ではない)。

## 5. 使い方 (想定フロー)

```bash
# クオンを対象に本番の写し世界を作る
python scripts/clone_world_to_test_env.py --persona quon_city_a

# 一日シム (実 LLM) をその世界で回す
SAIVERSE_HOME=test_data/.saiverse SAIVERSE_USER_DATA_DIR=test_data/user_data \
  python scripts/run_day_sim.py --scenario test_fixtures/scenarios/day_quon.json \
  --real --city <本番の CITY_SLUG> --db-file test_data/user_data/database/saiverse.db

# 状態の確認は検分 CLI (docs/intent/agent_inspection_cli.md)
python scripts/inspect_world.py day-plan quon_city_a --env test
```

判断点 playbook が本番 DB に import 済みならそのまま写る。未 import なら
従来どおり `scripts/import_playbook.py` を dest に向けて実行する (完了サマリで案内)。

## 6. スコープ外 (non-goals)

- 差分同期 (二回目以降も全消し全コピー。写し世界は使い捨てが原則)
- 本番への書き戻し (一方通行。書き戻しツールは作らない)
- expansion_data のコピー (リポジトリ共有なので両環境から同じものが見える)
