# 時間割まわり実機検証の統合手順 (2026-08-07)

**これは何**: 台帳に溜まっていた「検証待ち」のうち、**同じ一回のサーバー起動と一日の走行に相乗りできるもの**を集めて、時系列の一本の手順に束ねたもの。各項目の出典 doc への逆リンク付き。チェックを付けながら進める前提。

**この一巡で消化を狙う案件** (in_flight 台帳の行):

| 案件 | 出典 | 拾う場所 |
|---|---|---|
| 時間割の抜本改修 (T1〜T4+T2b) | [intent](../intent/timetable_redesign.md) §12-13 / [夜間ハンドオフ](2026-08-03_rss_and_timetable_night_handoff.md) §3 | Step 0〜3 |
| RSS フィード施設 | [夜間ハンドオフ](2026-08-03_rss_and_timetable_night_handoff.md) §2 | Step 1〜3, 5 |
| 自律行動v2 活性化配線 (再起動→夕方→就寝→編纂) | [intent](../intent/autonomous_behavior_v2.md) 経緯 | Step 1, 3 |
| ライフ改修A/B + Phase 4 + 案Y追従 | [life.md](../intent/life.md) | Step 1〜3 |
| track:N コマの空 Track 無音縮退 | [issue](../issues/track_slot_empty_degradation.md) | Step 3 |
| 判断プロンプトの静的一覧を head へ | [issue](../issues/judgment_static_lists_to_head.md) | Step 2〜3 |
| 判断点の席の競合制御 (コミット済み確認 2026-08-07) | [issue](../issues/judgment_seat_contention_and_event_loss.md) | Step 3 (受け身の観察) |
| v0.3.0 ④ オートノミー整理 (巻き取り+prune) | [worklist](../overview/v030_release_worklist.md) ④ | Step 1 |
| 実行台帳 W1/W2/W5 の正常系 + 横断ライフ一日 | [計画書](../overview/audit_remediation_plan.md) | Step 1, 3 |
| 起床設定変更×再起動の回復縁 | [issue](../issues/timetable_wake_change_recovery_edges.md) | Step 4 (別日) |

**ついでに拾える相乗り** (時間割とは別系統だが、同じ画面・同じ走行で見える):
Chronicle タブ全件表示 ([issue](../issues/arasuji_modal_500_limit_truncation.md)) / head に直近の記憶 ([issue](../issues/archive/chronicle_presentation_gap.md)) / 会話開始で「1件」ダイアログが出ない ([arasuji_levels](../intent/arasuji_levels.md) §13) / 送信前プレビューと開き直しの一致 (同 §15) / データ送信量セクションの状態表示化 ([issue](../issues/chat_options_metabolism_section_redesign.md)) / Memopedia 整理の `extraction-backlog` ログ ([issue](../issues/memopedia_writers_bypass_adapter_lock.md)) / 編纂の同名ページ輪 ([issue](../issues/curation_duplicate_pages_loop.md) — **編纂を再開する判断をした場合のみ**、Step 3 夜の部で一緒に見える)

---

## Step 0 — 起動前 (サーバー停止状態でやる)

- [x] **playbook が新版か確認**。(2026-08-08 済 — 自動同期でハッシュ一致を確認)起動時の自動同期 (`playbook_sync`) がファイルのハッシュ差分で自動更新するため、通常は再取込不要 (2026-08-07 実機で自動取込済みを確認)。例外は過去に `save_playbook` で DB 側を直接編集した Playbook (ユーザー優先でファイル上書きから保護される)。確認はこれで — `match: True` なら新版:

```bash
.venv/Scripts/python.exe -c "import sqlite3,json,hashlib,os;d=json.load(open('builtin_data/playbooks/public/judgment_day_open.json',encoding='utf-8'));h=hashlib.sha256(json.dumps(d,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()[:16];con=sqlite3.connect('file:'+os.path.expanduser('~/.saiverse/user_data/database/saiverse.db').replace(chr(92),'/')+'?mode=ro',uri=True);n=con.execute(\"SELECT nodes_json FROM playbooks WHERE name='judgment_day_open'\").fetchone()[0];print('match:',hashlib.sha256(json.dumps(json.loads(n),sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()[:16]==h)"
```

  一致しない場合のみ手動で: `.venv/Scripts/python.exe scripts/import_playbook.py --file builtin_data/playbooks/public/judgment_day_open.json`

- [x] **フロント再ビルド**。(2026-08-08 済)ライフ設定モーダル・経験タブ・kind バッジ・できごと畳み込み・フィードタブは全部ビルド後にしか見えない (:3000 は本番ビルド):

```bash
cd frontend && npm run build
```

- [ ] (任意) フィード配送を当日中に見たいなら `.env` に `SAIVERSE_FEED_FETCH_INTERVAL_SEC=60` (検証後は消す)

## Step 1 — 起動して、まずログを見る

`python main.py city_a` で起動 → `~/.saiverse/user_data/logs/<最新>/backend.log`:

- [x] **`[feed]` の取得ログ**が出る (フィード施設がまだ無い初回は「スキップ」で正常) — 2026-08-08 済 (interval=1800・初回即時)
- [ ] **オートノミー巻き取り**: `[handler:v0_3_0_dev4_retired_autonomy_...]` の INFO 行、または既に過去の起動で走っていれば何も出ない。裏取りはこれで (退役 playbook 名が残っていなければ合格):

```bash
.venv/Scripts/python.exe -c "import sqlite3,os;db=os.path.expanduser('~/.saiverse/user_data/database/saiverse.db');c=sqlite3.connect(db);print('schedule:',c.execute(\"SELECT COUNT(*) FROM persona_schedule WHERE META_PLAYBOOK IN ('track_autonomous','meta_autonomy_decision')\").fetchone());print('playbooks:',c.execute(\"SELECT COUNT(*) FROM playbooks WHERE name IN ('track_autonomous','meta_autonomy_decision','autonomy_creation','autonomy_web_research','autonomy_memory_organization','fragment_organize')\").fetchone())"
```

  両方 0 なら巻き取り+prune 完了。→ **2026-08-08 済** (ログは保持期間外で残っていないが DB 実体が 0/0/0。残るは「Pulse が正常に始まる」= 一日走行が兼ねる)
- [ ] **過ぎたコマの回復**: 起動時刻より前のコマが「流れた（サーバーが起動していなかったため）」と記録され、今の時刻のコマから合流する (できごと / 一日新聞で確認)。**過去コマの編成直後即時発火が起きない**こと
- [ ] **起動時に判断の空撃ちがない**: 開いている会話が無いペルソナに post_conversation 判断が飛ばない (案Y追従。ログに何日も前の会話への判断が出たら不合格)
- [ ] エラー・WARN の素通し確認 (W7 の起動時修復ログが出た場合は内容を控える)

## Step 2 — 画面だけで見られるもの (LLM 費用なし、起動中いつでも)

**時間割・ライフ系**:

- [x] コマ種別が新 7 種 (調べる/絵を描く/日記を書く/随筆を書く/出かける/自室で過ごす/自由時間)。旧種別の過去データ表示が壊れていない (2026-08-08 済)
- [x] **習慣テンプレート** (ペルソナメニュー、ライフ設定の隣): コマ行を作成・保存できる (2026-08-08 済。就寝時刻と衝突するコマが書ける件は縄張り issue へ実例追記)
- [x] **ライフ設定モーダル**: 起床・就寝・予算・モード上書きが 1 画面。予算欄に最低値ガイドが出る (2026-08-08 済。ただし習慣テンプレとの縄張り不可視 issue 起票)
- [x] **v1 亡霊の消滅**: 自律行動マネージャーの間隔入力・LifeView 最下部の間隔フォーム・DebugPanel の「自律 Pulse を 1 回」ボタンが消えている (2026-08-08 済)
- [x] ライフ帯が「**活動 N/M回**」表示 + 「**ふりかえり・判断: N回**」の別枠 (2026-08-08 済 — day_open 後も活動 0/2 のまま判断 1 回が別枠。初日の「4/4」再発なし)
- [x] 右サイドバーに**話しかけやすさチップ** (活動中/休憩中) (2026-08-08 済 — 表示機能は生きているが、部屋を出入りしないと更新されない stale 問題の影響下 → [issue](../issues/sidebar_autonomy_status_stale_after_start.md))
- [x] **経験タブ** (メモリーモーダル): エアの実データで索引・統計バッジ・薄い棚の減光・ページの動的合成が見える (2026-08-08 済 — 仕様通り表示。UI 要改善はアイディア帳へ)

**head (会話コンテキストのプレビューで)**:

- [x] 「**行ける場所**」の節 (現在地の直後あたり) と「**進行中のことと、やりたいこと**」の節 (生きる目的の直後) がある。Track が二度出ていない (2026-08-08 済。副産物: 現在地情報の二重を発見 → issue 起票)
- [x] (相乗り) head の末尾側に直近の記憶が戻っている / 送信前プレビューに厚い生ログが見え、開き直しと一致する / 二重表示なし (2026-08-08 済 — 意図通りの見え)

**フィード系**:

- [x] グローバル設定 → フィードタブ: 施設作成 (Building+プリセット選択) → 購読と健康状態が一覧に出る (2026-08-08 済 — ニュースアベニューにニューススタンド、3 購読とも最終取得成功。作成 UI のダークモード操作不能 issue / Building 同時作成の動線アイディアを記録)
- [x] 「今すぐ取得」→ 記事ビューアに当日の実在記事 (2026-08-08 済 — CNN 前日付の実在記事が要約つきで表示。まはー評「AI用のソフトとは思えない、ここだけでも役に立つ」。ペルソナ着弾分との一致確認は Step 3 のフィード配送で)
- [x] 素の URL (例 `publickey1.jp`) を貼って自動発見が働く (2026-08-08 済 — CNN トップページ URL からフィードを自動発見)

**その他の相乗り**:

- [x] Chronicle タブ: エリスの全 513 件が並び、切り詰めバナーが消えている (2026-08-08 済)
- [x] チャットオプション「データ送信量の管理」が読み取り専用の状態表示になっていて、水位バーとプレビューが整合 (2026-08-08 済 — 整合は目視レベル)
- [x] 会話を一度開始して「1件」ダイアログが出ない (2026-08-08 済)

## Step 3 — 一日の走行 (時系列。エアで回す想定)

前提: ACTIVITY_STATE が Active であること (スケジュール有効化だけでは発火しない)。

**朝 (day_open)**:

- [ ] 習慣テンプレの**枠どおり**に時間割が組まれ、穴 (「朝に決める」) だけが埋められている (一日新聞)
- [ ] 起床判断のプロンプトが痩せている (昨日のメモ・活動時間・予算・予定イベントだけ。行ける場所や Track 一覧が判断文に再掲されていない)
- [ ] 立てたばかりの Track も head に載り、翌朝のコマに割り当てられる

**日中 (作業コマ・出かけるコマ)**:

- [ ] 作業コマが走り、予算 (活動 N/M回) が 1 コマ 1 消費で減る。判断点は「ふりかえり・判断」に別枠記帳され、活動枠を食わない (実機初日の「4/4」の再発なし)
- [ ] **track:N コマ**: 生存タスクが無くても覚え書き (note) があれば縮退せず回り、指示書に note の意図が乗る。ログに `track slot degraded to presence` が**出ない**ことが合格
- [ ] **出かけるコマ**: 実際に公共施設へ移動し、軽い一手の独白が残る (文面は「出かけて、『◯◯』に来ました。」— 義務形・充填的長文になっていない)。移動失敗時に「出かけた」と**偽らない**
- [ ] **自由時間コマ**: 開始時に種別が選ばれる。選択失敗時は自室扱いに静かに縮退
- [ ] **コマ締め**: 作業コマ完了後、memory.db の root_theme 配下にテーマページ+fragment (経験値ノート) が書かれ、経験タブに反映される。帰属タグ (purpose_tags 層2) も付く。テーマページは**初経験の締めで初めて作られる** (事前一括生成されていない)
- [x] **フィード配送**: 出かけるコマ (またはスペル移動) でフィード施設の Building へ**到着した、その Pulse の中で**未読記事の知覚が入る (「[フィード] 『◯◯』の新着記事」)。記事ビューアの内容と一致 (透明性)。設置物欄 (visual context) に購読フィード名と直近見出しが出る。同じ記事が二度届かない (入室配送と定期サイクルの重複なし)。(2026-08-08 済 — ソフィーの出かけるコマで実証: 入室直後に 3 購読×3 件配送・カーソル前進、到着 Pulse の LLM 入力に設置物見出し 5 件と [フィード] 記事本文が入り、発話が実在記事 (ローマ暑さ対策=CNN) に言及。観察 3 点は [issue](../issues/feed_arrival_pulse_cannot_see_articles.md) の「実機検証の記録」節へ)
- [ ] 会話で割り込むとコマが繰り下がり、watchdog が騒がない

**夕〜夜**:

- [ ] 夕方の作業コマが走り成果物が実在する
- [ ] 暮らしコマは presence のみで正常 (kind バッジで見分けられる)
- [ ] 深夜帯 (コマなし時間) は静か。会話は夜でも貫通する

**就寝〜翌朝**:

- [ ] 就寝裁定が**営業日 (前日付) 基準**でふりかえる。予定と実際のズレに言及
- [ ] (編纂を再開する判断をした場合) 夜間の編纂一回分: 同名ページが増えていないか / 翌朝の「夜の間に棚の整理が行われました」/ 新聞の「棚の整理」欄
- [ ] (相乗り) 整理が走ったら backend.log に `extraction-backlog` の行

**一日を通しての横断観察**:

- [ ] 同じ判断が二重に走らない (day_open/day_close が同日 2 回出ない)
- [ ] `waiting:` 接頭辞の ERROR や台帳系の WARNING が平常時に出続けない (`rebasing` / `stale save` も同様)
- [ ] 一覧を変えた (施設追加・やりたいこと追加) 次の Pulse に変動通知が届いている

## Step 4 — 能動操作が要るもの (別日推奨)

起床設定の日中変更×再起動の回復縁 ([issue](../issues/timetable_wake_change_recovery_edges.md))。通常運用では発火しない縁なので、狙って作る:

- [ ] 日中に起床設定を跨ぎリズム (例: 23:00〜06:00) へ変更 → 深夜に再起動 → **翌朝、前日の残りコマが正しく扱われる** (回復 0 件で静かに消えない / 23:30 に day_open が撃たれたりしない)
- [ ] その最中、確定ライフの真っ最中に話しかけやすさが「未宣言」へ落ちない。判断点パルスが正しいライフに記帳される
- [ ] 谷に入ったら keep-alive が終わったライフを温め続けない (課金の出る経路)

## Step 5 — 終了時

- [ ] サーバー停止: 停止ログに feed worker の join が出て、例外なく閉じる

## 継続観察 (数日、手順ではなく観点)

timetable_redesign §12 の価値検証。エア相当 (作業好き) とアイフィ相当 (不活発) の二条件で数日:

- (a) 誤った知識が記憶されて次の前提になる連鎖が**ない** / (b) 全コマ接触ゼロの日が**ない** (フィード施設なしの City では供給源の細りを境界として明記) / (c) 再会時に渡せるもの (成果物・相談・共有話題) が実在する / (d) 習慣テンプレが LLM 出力で**変わっていない**
- 「日記を書く」と「随筆を書く」の違いがプロンプトだけで出るか (カタログ機構の検証実験)
- T3/T4 の文面トーン (day_plan.py / slot_close.py の実物をまはーの目で)

## この一巡に載せないもの (理由つき)

- **故障注入系**: W2 の crash 後 settle-close / W3 の dispatch 失敗 / W6 の memory.db ロック / 席のロック競合。狙って壊す実験は正常系の一日と混ぜない — 正常系が通ってから別枠で
- **まはー裁定待ち** (検証ではなく判断): §3-1 purpose_seed 分身モード / A3 (host/City 時差) / 実行台帳 list_unknown・list_dead の UI 置き場 / prefix キャッシュ保持の計測手段 (手段自体が未設計)
- **別系統の検証待ち** (この走行では踏まない): ComfyUI アドオン (ComfyUI 起動が要る) / OpenRouter ランキング (OpenRouter 経由の会話が要る) / SAIVerse Lite / ZIP インストール (次リリース時) / runtime_llm 分割スモーク (4 パターンの意図的な操作が要る — ただし Step 3 の通常会話で 1 パターン目は実質踏む)
