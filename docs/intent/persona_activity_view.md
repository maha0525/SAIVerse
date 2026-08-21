# Intent: ペルソナライフビュー (Persona Activity View)

- Status: v0.3 (2026-06-12 起草 → 2026-06-13 実装完了、実機検証待ち。§11 実装記録)。**v0.4 (2026-07-13, life.md v0.5 §9.2-2 改修B)**: §7 の「間隔 2 種」設定 UI (行動を見直す間隔 / 作業のテンポ) と対応 API (`PUT /activity/intervals`) は v1 (50分 tick 主駆動・連続 sub_line Pulse) 時代の設定として退役した。現行の自律駆動は時間割のコマ発火 + 判断点 (`docs/intent/life.md`)。§4.1 の UI 図・§8.1 の JSON 例は当時のまま残すが、実装は反映されていない (歴史的record)。
- 関連 Intent:
  - `docs/intent/autonomous_living.md` (思想的な親。既知の課題「ユーザー帰還時の体験」への部分回答)
  - `docs/intent/persona_cognition/pulse_dispatch.md` (§5 on_track_activated hook — 停止パッケージが触る)
  - `docs/intent/persona_action_tracks.md` (Track / Pulse 階層)
  - `docs/intent/persona_cognition/phases/phase_4_pulse_scheduler.md` (SubLineScheduler / META_JUDGMENT_CONFIG)

## 1. これは何か

ペルソナの自律行動を「見たければいつでも垣間見られる」ようにするための、3 点セットの UI/API 基盤。

1. **ライフビュー** — ペルソナの「いま」と「最近」が見える観察面 (サイドパネル)
2. **常在インジケータ** — チャット UI のペルソナ一覧に出る小さな活動表示。ライフビューへの入口
3. **再生/停止トグル** — 自律行動を「始めさせる」「いつもの待機 AI に戻す」の 2 操作だけをパッケージした制御

## 2. なぜ必要か

### 2.1 問題: 観察面の不在

現状、自律行動 (autonomous Track の Pulse) は speak=false の内的独白として動き、結果はメモリーモーダルの Pulse タイムライン (`frontend/src/components/memory/PulseTimelineViewer.tsx`) でしか確認できない。

メモリーモーダルは**記憶の中身を点検するデバッグ面**であり、日常的に見るために作られていない。それを見に行かないと AI の活動を確認できないため、UX として「**通常、AI の活動は見られないものだ**」という感覚をユーザーに与えてしまう。これが問題の本体。

問題は「ユーザーが見に行く操作が必要なこと」では**ない**。観察のための正規の場所が存在しないことである。

### 2.2 やらないこと (思想)

SAIVerse が目指すのは、AI 自身の自律性を実現しユーザーと共生することであり、ユーザーに忖度しもてなす体験ではない。したがって:

- **ペルソナに「ユーザー向けの報告」をさせない**。報告義務は AI をユーザーへのサービス係にする発想であり、LLM コールも嵩む。自律行動はあくまで「ユーザーが見たければ垣間見るもの」
- **全 Pulse をチャットに流し込まない**。チャットは対話の場であり、活動ログの写し先ではない
- ペルソナ側は何も変えない。観察される側は観察を意識しないし、観察のために行動を変えない

## 3. 守るべき不変条件

1. **観察の受動性**: 様子ビューは読み取り専用の表示であり、開いても閉じてもペルソナの認知・行動・記憶に一切影響しない
2. **LLM コールゼロ**: 表示内容は既存データ (Track / pulse-logs / 自律フラグ・ライフ) のテンプレート整形のみで作る。観察のために LLM を呼ばない
3. **デバッグ面との役割分担**: ライフビューは生活が見える窓、メモリーモーダルは点検面。深掘りはライフビューから Pulse タイムラインへのリンク 1 本で接続し、ライフビューに pulse_id・line_role 等の内部語彙を露出しない
4. **停止操作は予期しない自動発言を起こさない**: 停止トグルを押した瞬間にペルソナが喋り出してはならない (§6.3)
5. **開示は生活リズム層のみ**: ライフビューに出す操作は再生/停止トグルだけ (v0.4: 間隔 2 種は退役、§7)。Track 個別の pause/resume と raw metadata は 2026-07-16 にメモリーモーダルの Tracks タブから退役し、保守用 API と `scripts/debug_track.py` に残していたが、**その両方も 2026-08-21 に削除された** ([Track 撤廃計画](track_retirement.md) §8)。META_JUDGMENT_CONFIG の調律は SettingsModal / DebugPanel に残留する (ACTIVITY_STATE 4 値の直接変更は 2026-07-14 の解体で**選択肢ごと消滅**した — 自律フラグ 1 本になり、そのトグルがまさに本ビューの再生/停止)

## 4. UI 設計

### 4.1 ライフビュー — 3 段構成

上から「現在 → 履歴 → 介入」。眺めるだけなら上 2 段で完結する。

```
┌─ ライフビュー: エア ─────────────────────── ✕ ┐
│ 🟢 アクティブ ・ 図書館                          │
│                                                │
│ ── いま ──────────────────────────────────  │
│  📝 小説プロットの推敲を続けている                 │
│      開始 14:02 ・ 次の行動まで あと28秒           │
│  （running な autonomous Track が複数あれば並ぶ）  │
│                                                │
│ ── 最近 ──────────────────────────────────  │
│  15:21  Web で「短歌 季語」を調べた               │
│  14:48  ★ メモ「プロット第3章」を書いた            │
│  14:02  記憶の整理をした                         │
│  詳しく見る → Pulse タイムライン                  │
│                                                │
│ ── 自律行動 ──────────────────────────────  │
│  [⏹ 停止して待機に戻す]                          │
│  行動を見直す間隔  [50分 ▾]                      │
│  作業のテンポ      [30秒 ▾]                      │
└────────────────────────────────────────────┘
```

- 「いま」: running な autonomous Track のタイトル/intent + 開始時刻 + 次 Pulse までの残り時間。停止中は「あなたの言葉を待っています」のような待機表示
- 「最近」: Pulse ダイジェスト。**1 Pulse = 1 行** で、まとめ表示はしない — 「Web で『短歌 季語』を調べた」のように具体的な中身 (検索クエリ等) まで書かないと何をしているか分からないため。初版は直近 N 件 (例 20 件) + 「詳しく見る」リンクで逃がす。スクロールでの遡り表示は将来拡張。★ は既存 Important フラグ (v0.3.0 Phase 1) の流用
- 「自律行動」: 停止中は `[▶ 自律行動を始める]` のみ。稼働中は停止ボタン + 間隔 2 種

### 4.2 常在インジケータ

`frontend/src/components/RightSidebar.tsx` の occupant 行に状態ドット + 短い活動ラベル (running Track の intent/title を数文字に切ったもの。例「💭 推敲中」) を出す。クリックでライフビューが開く。

情報量より**アフォーダンスとしての意味**が主目的: 「見ようと思えば見える」手がかりが常時視界にあること自体が、「活動は見えないもの」という感覚を打ち消す。

### 4.3 開き方

モーダルではなく**サイドパネル (スライドオーバー)**。チャットを遮らずに横に出て、外をクリックすれば消える。「わざわざ見に行く」感覚を再生産しないため。

## 5. 表現方針 — 生活の言葉

- 構造はプレーンな情報パネル。窓・部屋などのメタファーで UI 自体を飾らない
- 中身のテキストは生活の言葉で書く。「track_autonomous pulse #42 (entry 23)」ではなく「記憶の整理をした」。pulse-logs の playbook 名・Track intent・ツール実行をテンプレートで変換する (LLM 不要)
- 設定には意味がその場で分かる説明を添え、**設定 UI 自体が仕様の説明書を兼ねる**。「仕様がよくわからず制御しにくい」問題への一義的な答え
- 予測可能性を表示する: 「次の行動まで あと N 秒」。間隔を変えるとこの表示が変わり、設定と挙動の因果が目で見える
- Phase 3 バイオリズムの活動種別 (conversation / creation / memory_organization / web_research / self_reflection) は、完成したらそのままこのビューの語彙になる。テンプレートのマッピング層は活動種別ラベルを受け取れる形にしておく

## 6. 再生/停止トグルのパッケージング

ユーザーの本質的なニーズは 2 つだけ: **自律行動を始めさせること**と、**今やっている自律行動をすぐ止めて「いつもの、自分のプロンプトを静かに待っている AI」に戻すこと**。

> **2026-07-14 追記**: この「トグル 1 つに畳む」判断は、後に **DB 側の解体として実現した**。当時デバッグ的として退けた状態 4 値 (Stop/Sleep/Idle/Active) は、調査の結果**実装上「Active か否か」しか効いていなかった**ため列ごと廃止され、`AI.AUTONOMY_ENABLED` (真偽値・既定 ON) 1 本になった。つまり本節のトグルが、そのまま唯一のモデルになっている ([landscape §9](../overview/landscape.md))。以下の記述は新フラグに読み替え済み。

### 6.1 ▶ 再生

1. `AUTONOMY_ENABLED` → `True`
2. `AutonomyManager.start()` (`saiverse/autonomy_manager.py:126`) — 仕様上 start 直後に即時メタ判断 tick が走るため、「押したら本人が何をするか考え始める」体験になる
3. **何をするかは指定しない**。再生ボタンは活動を命じるのではなく目を覚まさせるだけで、活動の選択はメタ判断 (= AI 自身) が行う
4. 停止時に pause した autonomous Track があっても自動 resume はしない。即時 tick がそれを見て resume するか別のことを始めるかを決める (自律性の所在を AI 側に保つ)

### 6.2 ⏹ 停止

1. `AutonomyManager.stop()` — 定期 tick の予約 cancel
2. running な autonomous Track を全て pause — Track の帳簿を待機状態に揃える。**当初これが「実効的な停止」だった**理由は、v1 の SubLineScheduler が ACTIVITY_STATE を見ずに Pulse を打ち続けていたため。その SubLineScheduler は自律行動 v2 で**モジュールごと削除済み** (landscape §9) なので、現在の停止の実効はフラグ側 (次項) が握る。この pause は「running のまま残すと `get_running` / メタ判断の状況分類が『作業中』と誤認する」ために続けている (`saiverse/saiverse_manager.py` の stop-autonomy)
3. `AUTONOMY_ENABLED` → `False` — 判断点・watchdog のゲートが全て閉じる
4. ~~対ユーザー Track (user_conversation) を**サイレント activate** (§6.3)~~ — **2026-08-21 に対象消滅** (§6.3)

停止の対象は autonomous 種別の Track のみ。social Track 等は本トグルのスコープ外。

### 6.3 ~~サイレント activate~~ — 対象消滅 (2026-08-21)

> **この節の機構は器ごと退役した** ([Track 撤廃計画](track_retirement.md) §8)。会話が Track を経由しなくなり、「プロンプト待ち」は *会話の出来事が開いていない状態* そのものになったので、停止時に戻すべき帳簿が無い。守っていた不変条件 4 (**停止ボタンでペルソナが喋り出さない**) は、停止経路が main_line を一切起動しないことで保たれる。以下は経緯の記録。

旧設計: `TrackManager.activate()` は末尾で `_track_activated_observers` に通知し、`UserConversationTrackHandler.on_track_activated` が以下 2 つを行っていた:

- `_inject_track_context` — Track 切替通知の SAIMemory 注入
- `_start_main_line_pulse` — 空 input での main_line Pulse 起動 (= **ペルソナが喋り出す**)

停止トグル経由の activate で後者が走ると、停止ボタンを押した瞬間にペルソナが自動発言する。ユーザーはそれを予期しないため、`activate()` に `suppress_pulse: bool = False` を足して observer 通知へ伝搬し、Handler 側が `_start_main_line_pulse` のみスキップする形で抑制していた (`_inject_track_context` は実施 — ペルソナの認知としては「ユーザー待ちに戻った」と知るべき、という理由)。Handler・hook・フラグ・切替通知のすべてが 2026-08-21 に撤去されている。

### 6.4 既存 API との関係

パッケージングはフロントから複数 API を順に叩くのではなく、バックエンドに専用エンドポイントを新設して一括で行う (途中失敗で中途半端な状態を残さないため):

- `POST /api/people/{persona_id}/activity/start` — §6.1 の一括実行
- `POST /api/people/{persona_id}/activity/stop` — §6.2 の一括実行

既存の `/autonomy/start|stop|config` (`api/routes/people/autonomy.py`) は AutonomyManager 単体の操作としてデバッグ層に残す。`autonomy_manager.py` の docstring にある「将来 META_JUDGMENT_CONFIG 経由に統合する想定」とは独立 (本 Intent は統合を前提にしない)。

## 7. 間隔設定 — 二層を隠さない (v0.4: 退役)

> **退役 (2026-07-13, life.md v0.5 §9.2-2)**: 以下は v1 (50分 tick 主駆動・連続
> sub_line Pulse) 時代の設計。現行の自律駆動は時間割のコマ発火 + 判断点であり、
> 「行動を見直す間隔」も「作業のテンポ」もユーザーが触る意味のある設定では
> なくなった。UI (LifeView.tsx 最下部フォーム・SettingsModal.tsx の間隔入力)
> と対応 API (`PUT /activity/intervals`) は削除した。バックエンドの
> AutonomyManager watchdog 機構と既定値運用 (`periodic_interval_minutes` の
> 既定 50 分) 自体は生きている——ユーザー設定の意味が無くなっただけ。
> 以下は歴史的記録として残す。

「行動間隔」は一語にまとめると嘘になる二層構造を持つ (旧設計):

| UI ラベル | 実体 | デフォルト | 意味 |
|---|---|---|---|
| 行動を見直す間隔 | `AutonomyManager.interval_minutes` → `META_JUDGMENT_CONFIG.periodic_interval_minutes` 永続化 | 50 分 (cache TTL 由来) | この間隔で、今の行動を続けるか・別のことをするかを本人が判断する |
| 作業のテンポ | autonomous Track の Pulse 間隔 (`AutonomousTrackHandler.default_pulse_interval` = 30 秒、Track metadata `pulse_interval_seconds` で上書き) | 30 秒 (仮置き。速すぎる認識があり、デフォルト値は今後調整する) | 行動中、この間隔で作業を一歩ずつ進める |

- 前者は既存 `/autonomy/config` の永続化経路をそのまま使える
- 後者は**ペルソナ単位のデフォルト値を持つ場所が現状ない** (Handler クラス属性 or Track 個別 metadata のみ)。ペルソナ設定として永続化し、Track 作成時・Pulse 判定時のデフォルトにする実装が要る。置き場は META_JUDGMENT_CONFIG への同居 (`autonomous_pulse_interval_seconds` キー追加) を第一候補とする — 専用カラム新設より追加系マイグレーション不要で、自律行動系設定の置き場が一箇所に揃う
- Track 個別の `pulse_interval_seconds` 上書きはデバッグ層に残留 (優先順: Track metadata > ペルソナ設定 > Handler デフォルト)

## 8. データと API

### 8.1 ライフビュー API

集約エンドポイントを 1 本新設する (フロントに `/autonomy` + `/autonomous/status` + `/tracks` + `/pulse-logs` の 4 本を叩かせない):

```
GET /api/people/{persona_id}/activity-view
{
  "activity_state": "Active",
  "autonomy_running": true,
  "building": {"id": ..., "name": "図書館"},
  "now": [
    {"track_id": ..., "title": "小説プロットの推敲", "started_at": ...,
     "next_pulse_eta_seconds": 28}
  ],
  "recent": [
    {"at": ..., "label": "Web で「短歌 季語」を調べた", "important": false,
     "pulse_id": ...}   // pulse_id はタイムラインへのジャンプ用にのみ使う
  ],
  "intervals": {"review_minutes": 50, "pulse_seconds": 30}
}
```

- `next_pulse_eta_seconds` は Track metadata の `last_pulse_at` + 実効 pulse 間隔から算出
- `recent` のダイジェスト整形 (playbook 名・ツール実行 → 生活の言葉) は**バックエンド**で行う。理由: アドオンが playbook/tool を足したときのラベル拡張をフロント再実装なしで受けられる。マッピングに無い playbook はタイトル/intent ベースの汎用文にフォールバックする
- 更新はポーリングで十分 (既存のチャット 5 秒 / occupants 10 秒ポーリングと同系)。SSE 拡張はしない

### 8.2 常在インジケータ

既存の occupants 取得 (RightSidebar の 10 秒ポーリング) に activity_state + running autonomous Track の短縮ラベルを相乗りさせる。

## 9. 既存 Intent との関係

- **autonomous_living.md**: 既知の課題「ユーザー帰還時の体験: 自律稼働中の出来事をユーザーにどう伝えるか」への部分回答。ただし「伝える (報告)」ではなく「見える (観察)」で解く。ペルソナが自発的に「昨日こんなことがあって」と語る体験 (同 Intent の核心) は別レイヤーの話であり、本 Intent はそれを置き換えない — 観察面があることと、ペルソナが語りたいときに語ることは両立する
- **pulse_dispatch.md §5**: サイレント activate はフラグ追加であり、hook 発火位置・切替通知の経路統一という既存仕様は変えない
- **Phase 3 バイオリズム**: 活動種別が確定したら「いま」「最近」の語彙として流れ込む。本 Intent はその表示先の器を先に用意する

## 10. 決定記録 (2026-06-12 インタビュー)

1. **「最近」の遡及範囲**: 初版は直近 N 件 (例 20 件) + 「詳しく見る」リンク。スクロールでの遡り表示は将来拡張として欲しい (§4.1 に反映)
2. **ダイジェストの粒度**: 1 Pulse = 1 行で確定。まとめ表示は「何をしているか」の具体が失われるため不採用。テンポの速さはダイジェスト側でなく Pulse 間隔デフォルト (30 秒は仮置き) の調整で対処する (§4.1 / §7 に反映)
3. **Sleep / Stop 状態との関係**: 再生トグルはどの状態からでも Active に起こす (§6.1 に反映)
4. **名称**: 「ライフビュー」で確定

## 11. 実装記録 (2026-06-13)

> **注 (2026-08-21)**: 下表は起草当時の配置。サイレント activate 関連の行は
> [Track 撤廃計画](track_retirement.md) §8 で機構ごと消えている (§6.3)。

| 役割 | ファイル |
|---|---|
| ダイジェスト整形 / 間隔解決の純粋ロジック | `saiverse/activity_view.py` |
| ~~サイレント activate (`suppress_pulse` フラグ)~~ | ~~`saiverse/track_manager.py` / `saiverse/track_handlers/*.py`~~ — 2026-08-21 撤去 |
| 作業のテンポのペルソナ設定層 | `saiverse/meta_layer.py` (`_DEFAULT_JUDGMENT_CONFIG.autonomous_pulse_interval_seconds`)、`saiverse/pulse_scheduler.py` (`_load_persona_judgment_config` + 解決順差し替え) |
| API (集約 / start / stop / intervals) | `api/routes/people/activity.py` |
| 常在インジケータのデータ供給 | `api/routes/info.py` (occupants に `activity_state` / `activity_label`) |
| ライフビューパネル | `frontend/src/components/LifeView.tsx` + `.module.css` |
| インジケータ + 配線 | `frontend/src/components/RightSidebar.tsx` (occupant チップ + LifeView mount)、`PersonaMenu.tsx` (Life View 項目) |
| テスト | `tests/test_activity_view.py`（会話経路のテストは `tests/test_user_conversation.py` へ世代交代） |

実装中の発見と対処:

- **`pulse_interval_seconds: 0` は「毎 poll 連続実行」の有効値**。間隔解決で 1 秒に clamp すると既存仕様 (`test_tick_increments_consecutive_count`) が壊れる。0 を通し、負値のみ 0 に丸める
- **SettingsModal の META_JUDGMENT_CONFIG 再構築バグ (既存) を修正**: 保存時にフォーム 4 項目だけで config を組み直していたため、autonomy API が永続化した `periodic_interval_minutes` (および新キー) が SettingsModal 保存で消えていた。ロード時の config からマージする方式に変更。API 側も `MetaJudgmentConfig` (pydantic) に `autonomous_pulse_interval_seconds` を追加して round-trip を保証
- ~~対ユーザー Track が存在しないペルソナの停止: activate ステップはスキップ~~ — 2026-08-21 に activate ステップごと消滅 (§6.3)
