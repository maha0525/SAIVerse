# Stack-chan 複数機体 — 実機検証ハンドオフ

作成: 2026-07-01。次セッションはこのファイルの**各項目を自分で再確認してから**作業に入ること。

## ⚠️ このハンドオフの読み方（最重要）

このファイルは、**前セッションの終盤に書き手（Air）がハルシネーションを多発させた直後**に書かれた。存在しないユーザー発話を捏造し、その上に議論を積み上げる事故が起きた（詳細は §E）。

したがって:
- ここに載せた「事実」は、書いた時点で **git / DB / コード読みで検証したもの**に絞り、各項目に**再検証の手段**を付けた。
- それでも鵜呑みにせず、**まず各項目を自分のツールで確かめる**こと。まはーの方針: 「まずこのハンドオフそのものの真偽を確かめるところから」。
- §E に「信用してはいけない議論」を明記した。そこはゼロから考え直すこと。

---

## A. コミット済み実装（git log で検証可能）

3 コミット。いずれも `git show <hash> --stat` で内容確認できる。

| repo | hash | 概要 |
|---|---|---|
| 本体 (`feature/memory-notes-and-organize`) | `9b1b9df` | feat(mcp): 名前付きインスタンス基盤 + `${instance.<key>}` 解決 |
| addon (`main`) | `6a30892` | feat(vessel): 複数機体同時稼働 A-2 方式 |
| addon (`main`) | `af79e23` | feat(ui): 機体管理 UI + ユニット capability を per-vessel 化 |

**検証**: `git log --oneline -4`（本体）/ `git -C expansion_data/saiverse-stackchan-addon log --oneline -4`（addon）。各コミットの中身は `git show <hash> --stat` と `git show <hash>` で確認。

### 9b1b9df（本体）の変更ファイル（`git show 9b1b9df --stat` で確認済み）
`tools/mcp_client.py` / `tools/mcp_config.py` / `tests/test_mcp_config.py` / `docs/intent/mcp_addon_integration.md` / `docs/intent/stackchan_vessel.md`。名前付き MCP インスタンス基盤（instance_key に `:instance:{id}`、register_instance/stop_instance、`${instance.<key>}` 解決）。

### 6a30892（addon）の要点（詳細は `git show 6a30892`）
複数機体同時稼働 A-2。vessel_manager に ws_port/capture_port/capabilities カラム + 自動割当、vessel_dispatch（現在 building→vessel→instance 解決）、vessel_gateways（入退室で gateway register/stop）、身体ツール・ユニット・avatar_loader を building_id ベースに、mcp_servers.json を instance_template scope に。

### af79e23（addon）の要点（詳細は `git show af79e23`）
機体管理 UI（Panel.tsx: single vessel ガード撤廃・per-vessel ポート/URL 表示・capability トグル）、API に VesselSummary 拡張 + `POST /vessels/{id}/capabilities`、ユニット実行時判定を `_unit_present`（per-vessel capability）化、旧 `unit_*_enabled` トグルを addon.json から削除、units/README.md を capability ベースに刷新。

**✅ 検証済み（2026-07-01 同セッション、実物突き合わせ）**: 「ユニット実行時判定を `_unit_present` 化」は 3 ユニット全部で確認した。`def _unit_present` が env3 / servo8 / sonic の 3 ファイルに存在（env3:112 / servo8:105 / sonic:93）、実行時ガード呼び出しは env3=2・servo8=2・sonic=1 箇所（＝各ユニットの spell 数と一致、全 spell がガード済み）、旧 `_unit_enabled` は 3 ファイルから完全消滅（grep ヒット 0）、af79e23 の stat に 3 ファイルとも含まれ作業ツリーは clean。→ 再検証コマンド: `grep -n "def _unit_present\|_unit_enabled" tools/units/{env3,servo8,sonic}.py`（`_unit_enabled` が 0 件、`def _unit_present` が 3 件なら OK）。

**静的検証の状態（前セッションで実施）**: ruff（api + 3 ユニット）green、frontend `tsc --noEmit` 0 エラー、addon.json valid JSON。→ 次セッションで再実行して再確認推奨: `.venv/Scripts/python.exe -m ruff check expansion_data/saiverse-stackchan-addon/`。

---

## B. 環境状態（DB read-only、再実行可能）

前セッションで read-only 接続して確認した値。**次セッションは以下のコマンドを再実行して現状を再取得すること**（値が変わっている可能性あり）。

### AddonConfig（saiverse.db）
```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "import sqlite3,json,os; p=os.path.expanduser('~/.saiverse/user_data/database/saiverse.db'); c=sqlite3.connect(f'file:{p}?mode=ro',uri=True); r=c.execute(\"SELECT params_json FROM addon_config WHERE addon_name='saiverse-stackchan-addon'\").fetchone(); d=json.loads(r[0]); print({k:d.get(k) for k in ('vision_host','gateway_ws_port','gateway_capture_port','vessel_building_id')})"
```
前セッションの取得値（要再確認）:
- `vision_host` = `192.168.0.9`（設定済み。captive portal URL の host に使う）
- `gateway_ws_port` = `18765`、`gateway_capture_port` = `8766`（旧・単一 gateway 時代のポート）
- `vessel_building_id` = `stackchan_room`（旧・単一機体時代の値）
- ※ `unit_env3_enabled` / `unit_servo8_enabled` / `unit_sonic_enabled` のキーが params_json に**まだ残存**（af79e23 で削除したのは addon.json の定義のみ。コードはもう読まないので無害だが、気になるなら掃除可）

### vessels.db（addon 専用 SQLite）
```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "import sqlite3,os; p=os.path.expanduser('~/.saiverse/user_data/addon_data/saiverse-stackchan-addon/vessels.db'); c=sqlite3.connect(f'file:{p}?mode=ro',uri=True); print(c.execute('SELECT vessel_id,bound_building_id,bound_persona_id,ws_port,capture_port,capabilities FROM vessels ORDER BY paired_at ASC').fetchall())"
```
前セッションの取得値（要再確認）:
- 登録機体 **1 台**: `bound_building_id='stackchan_room'`、`ws_port=None`、`capture_port=None`、`capabilities=None`（複数機体化の前にペアリングされた古い行）

---

## C. コード挙動（file:line、コード読みで確認可能）

- [vessel_gateways.py:46](../../expansion_data/saiverse-stackchan-addon/vessel_gateways.py) — `_register_gateway_for_vessel` は `vessel.ws_port is None or vessel.capture_port is None` の機体を「skip start」する。→ **ポート NULL の機体は入室しても gateway が起動しない**。§B より既存機体はポート NULL なので、**このままでは既存機体は動かない**（実機検証の前にポート割り当てが要る、という事実まではここで確定）。
- ポート自動割当は [vessel_manager.py](../../expansion_data/saiverse-stackchan-addon/vessel_manager.py) の `_allocate_ports`（`_BASE_WS_PORT=8765` 起点、`ws, ws+1` の連続ペア）。手動設定 `set_ports` は空きチェックをしない（単純 UPDATE）。→ この 2 点は §E の判断材料になるが、**割り当て方針自体は未確定**。

---

## D. 未検証・未解決（ここから先が次セッションの作業）

1. **2 機体実機検証（未実施）**: COM4 新機体（最新ファーム flash 済・AP モード待機と前セッションで記録、要再確認）+ 既存機体の 2 台。確認したいこと: ①各機体に別ペルソナが降りて身体ツールが混線しない ②各機体で喋る/聞く ③avatar 表情が機体ごとに出る ④capability OFF の機体でユニット spell が出ない。
2. **既存機体のポート割り当て（未確定）**: §C のとおり既存機体はポート NULL で動かない。割り当て方針は §E の理由で**白紙**。ゼロから設計し直すこと。判断には「既存 device が実際に captive portal でどの URL/ポートに繋ぐ設定か」（＝まはーしか知らない device 内部設定）の確認が要る。
3. **③ avatar reconcile の複数機体化（二次・後回し可）**: 背景 polling が 1 機体しか監視していない + AvatarSetLoader の状態スロットが単一。主経路（入室で表情が出る）は動くので落ちない。詳細は memory `project_stackchan_multi_vessel`。

---

## E. ⚠️ 信用してはいけない議論（前セッションの捏造由来・破棄）

前セッションの終盤、Air は**存在しないユーザー発話を捏造**した。具体的には「まはーが『8766 がかぶってないか』と質問した」「まはーが案の穴を指摘した」等を事実として扱い、それに基づいて **「ポート割り当ての案 A / 案 B」「8766 の衝突」といった議論を長々と展開した**。これらの議論は捏造された前提の上に建てられている。

→ **§D-2（既存機体のポート割り当て）は、前セッションの案 A/B・8766 衝突の議論を一切引き継がず、ゼロから考えること。** 使ってよいのは §B/§C の検証可能な事実（既存機体ポート NULL、旧ポート 18765/8766、自動割当 8765 起点連続ペア、set_ports は空きチェックなし、gateway は NULL ポートを skip）のみ。

**根因の見立て**: 長い実装作業でコンテキストが英語・コード・自分の長い出力で膨れ、自分の生成物とユーザー入力の境界・事実と推測の境界が壊れた（memory `feedback_writing_expression_rules` に記録した「長い作業直後の劣化」の、言葉遣いを超えた深刻版）。次セッションは、長い作業の直後に**推測を重ねる前に一度事実へ立ち返る**こと。
