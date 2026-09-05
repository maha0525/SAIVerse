# Metabolism / Anchor（節目）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §6](../overview/landscape.md)、**設計意図**は intent [`cache_lifecycle_control.md`](../intent/cache_lifecycle_control.md) / [`cached_head_architecture.md`](../intent/cached_head_architecture.md) を参照。

## 一言で

[Session](session.md)（短期記憶）が継続不能になると発火する節目のイベントが **Metabolism**、その起点を指すマーカーが **Anchor**。

## 役割

Metabolism は短期記憶を区切り直す節目であり、同時に**短期記憶と長期記憶をつなぐ**。ここで短期記憶（[head](session.md) snapshot）を再構築しつつ、長期記憶への結晶化（[Chronicle](chronicle.md) 圧縮・[Memopedia](memopedia.md) Fragment 生成）を束ねて実行し、新しい Session を開始する。

## Metabolism（短期リフレッシュ + 長期結晶化）

### 発火条件

**提示コンテキストの予算超過** = 上限超え / トークン閾値超え。これが自動発火の**一本**（intent [`arasuji_levels.md`](../intent/arasuji_levels.md) §3/§13、2026-07-29）。cache TTL 切れは発火条件では**ない** — TTL は「キャッシュが冷えた」という温度情報にすぎず、提示範囲を変えない（§13 裁定1。旧「TTL 失効で最小ロード + 会話前編纂」は撤去済み）。

**context 過剰の判定は文字数の二数** = 上限と残す量（2026-07-28、intent [`arasuji_levels.md`](../intent/arasuji_levels.md) §9）。旧「モデルごとのメッセージ数」は 2026-07-25 に単位ごと廃止（三水位へ）、三水位の低水位は 2026-07-28 に eviction から未使用化（残す量が保護を兼ねる）を経て、**2026-09-04 に廃止**（最後に残っていた役割 = anchor 未確立時の初期読み込み量は残す量を流用する。発話直前の最終防衛ライン §15-5 が窓を残す量まで埋め直すため、独立した値として効く場面が無かった — [issue](../issues/watermarks_unsatisfiable_when_perception_is_large.md) 裁定 5）。モデル JSON や旧 DB に残る `metabolism_low_chars` キーは黙って無視される。

| 数 | 意味 | 組み込み既定 | モデル設定キー |
|---|---|---|---|
| 残す量 | 畳んだ後に残す直近の**会話の行**の文字数 = 保護範囲。**主語は会話の行だけ**（知覚ブロックは消費しない — 2026-09-03 裁定。スペル結果・通知などの機構名義の行も消費しない — 2026-09-04 裁定。機構行は畳みの対象には含まれたまま、数えないだけ）。上限との差がバッファで、発火は「たまに・まとめて」になる（キャッシュ保護）。anchor 未確立（新規ペルソナ / 修復直後）の初期読み込み量もこの値 | 40,000 | `metabolism_target_chars` |
| 上限 | これを超えたら発火（未設定 = null なら `token_triggered` のみで発火）。**主語は実際に送る合計**（会話の行 + 知覚ブロック） | 120,000 | `metabolism_high_chars` |

**知覚には知覚の上限がある**（2026-09-04 裁定、実装 2026-09-05）。上の二数は会話を畳むことで動かせる量の水位だが、送信直前に差し込まれる知覚ブロック（部屋の様子・通知）は畳みの対象ではないので、知覚が多いと「会話を残す量まで畳んでも合計が上限を下回らない」状態が生まれる（[issue](../issues/watermarks_unsatisfiable_when_perception_is_large.md)）。そこで知覚の合計にも二水位を置く — 組み込み既定 **6万（上）/ 4万（下）**、モデル設定キーは `perception_high_chars` / `perception_target_chars`。この二つも下表の三層で決まる。上を超えたら古い側を下までまとめて提示から下ろし、その境界は一方向にしか進まない（下ろしたものは戻らない = キャッシュの割れは一回きり）。下ろした跡地には機構名義の一行（`[省略された記録]` + 件数）が出る。件数は下ろした**記録の数**であってバッチ（1 回の知覚）の数ではない — 1 回の知覚は、その時点で溜まっていた記録を全部束ねるため。台帳は無傷なので、その期間の編纂が来れば材料として引き取られる。設計は intent [`perception_buffer.md`](../intent/perception_buffer.md) §10.9。

**水位は三層で決まる**（2026-09-03）: **組み込み既定 < 全体設定 < モデル定義**。

| 層 | どこで編集するか | 保存先 | 意味 |
|---|---|---|---|
| 組み込み既定 | 編集しない | `saiverse/model_configs.py` の `BUILTIN_METABOLISM_*_CHARS` / `BUILTIN_PERCEPTION_*_CHARS` | 何も設定していないときの値（上表） |
| 全体設定 | 全体設定画面 → 環境タブ「ペルソナに送る量の水位」（`GET/PUT /api/config/metabolism-defaults`） | `user_settings.{METABOLISM,PERCEPTION}_{TARGET,HIGH}_CHARS`（NULL = 未設定） | モデル定義にキーが無いモデルが従う既定。多くの利用者には組み込み既定が大きすぎるため、全モデルまとめて下げる入口 |
| モデル定義 | モデル編集 UI の専用欄（`metabolism_*_chars` / `perception_*_chars`） | モデル JSON | 数値を書いたモデルはそれが勝つ。null = その水位を持たない = Metabolism なし／知覚を下ろさない（モデル単位のオプトアウト。全体設定では表せない） |

解決は `saiverse/model_configs.py` の `resolve_metabolism_watermarks` / `resolve_perception_watermarks` の二箇所（どちらもキー無し → 全体設定 → 組み込み、キーあり null → None。全体設定は不変の写像を一枚だけ読み、そこから水位をまとめて解くので、差し替えの途中で新旧が混ざった組にはならない）。四つの水位を一枚の写像に同居させているのは、下の保存時検査が二族をまたぐため。全体設定の真実は DB で、起動時（`saiverse_manager`）と保存成功時（API）に `set_global_watermark_defaults` でモジュール変数へ写すので再起動は要らない。

**保存時検査**（`api/routes/config.py` の `_watermark_constraints_error` 一本。モデルの作成・更新・複製・チャットからの保存・全体設定の PUT が全部ここを通る）は三つ:

1. **型**: 数値でも null でもない値を弾く（実行時は既定へ黙って落ちるが、保存の入口では教える）。
2. **順序**: 残す量 ≤ 上限（Metabolism と知覚でそれぞれ）。空欄のキーは**全体設定込みの実効既定**で埋めて比べる。
3. **余裕**（2026-09-05、[裁定 4](../issues/watermarks_unsatisfiable_when_perception_is_large.md)）: **整理をはじめる量 − 残す量 > 知覚の上限 + 余裕**（余裕は `WATERMARK_HEADROOM_CHARS`、現 1万字）。これが無いと「会話を残す量まで畳んでも、知覚の分だけ合計が上限を超えたまま」という**設定として満たせない状態**が保存できてしまう。等号ぎりぎりを許さないのは、畳んだ後の会話が端数（材料 U 未満の畳み残し）で残す量ちょうどには収まらないため。どれかの水位が null（＝その水位を持たない）なら、その約束自体が無いので検査しない。

逆向きも守る: 全体設定の変更が、水位の一部だけを書いている既存モデルの実効の組を壊す（例: target=25万 だけ書いたモデルがあるのに全体の high を 12万 に戻す）ときは保存を拒み、エラーはそのモデル名を挙げる（先にモデル側を直すか空欄にする）。既に保存済みの壊れた組は起動時に直さない — 実行時の縮退（水位逆転のクランプ、知覚の「最新 1 件は下ろさない」+ 警告）が受け止める。

2026-07-30 に撤去した「水位のグローバル上書き」（`POST /api/config/metabolism`）とは別物 — あれは会話ごとの画面に置かれた揮発性の一本の上書きで、効く範囲が画面から読めなかった。今回の全体設定は全体設定画面に置かれた永続の**既定**で、モデル定義が明示的に上書きできる。

数え方は**提示される提示コンテキストの文字数**（圧縮区間は digest に置き換わった後の量）。2026-09-02 のまはー裁定で、数える単位は「**実際に送る中身**」= 保存済みの会話行 + 送信直前に差し込まれる知覚（部屋の様子。保存行を作らないため勘定から丸ごと漏れていた — [issue](../issues/archive/context_accounting_excludes_injected_rows.md)）。ただし**合計を見るのは上限（発火）側だけ**（応答後の自動発火、非常畳み、先回り畳みの中間値、被覆補修の `fold_needed`、context-status の `presented_chars`）。**残す量（保護範囲）側は会話の行だけ**を見る — 退場計画の保護境界、§15 読み戻しの不足判定と予算、§15-3 印戻しの止め時、手動整理の門、「印戻しだけで収まったか」の早期完了、被覆補修の退場境界（2026-09-03 まはー裁定。2026-09-02 の統一が保護範囲にまで及んで会話を畳みすぎた事故 — [issue](../issues/protection_quota_consumed_by_perception_blocks.md)）。合計が上限を超えているのに会話の行が残す量以下なら畳めるものは無く、整理は LLM を呼ばず引き返す（警告はペルソナごと 1 度、context-status の `perception_over_budget` / `window_rows_chars` に出る）。「材料の量」で測るのはあらすじの被覆 U の判定だけで、そちらは従来どおり（2026-08-29 裁定）。水位は上の三層（組み込み既定 < 全体設定 < モデル定義）で決まり、モデル定義の null = その水位を持たない = Metabolism なし が唯一のオプトアウト。旧グローバル上書き（`POST /api/config/metabolism`）と ON/OFF トグルは 2026-07-30 に撤去（landscape §9）。現在量は `GET /api/people/{id}/context-status`（read-only、§15 読み戻し込み）でチャットオプションに表示される。

### 実行点（予算超過の一本 + 手動 + §14 の保守経路）

| 実行点 | 場所 | 発火条件 |
|---|---|---|
| **応答後（自動）** | `sea/runtime.py` run_meta_user 末尾 → `maybe_run_metabolism` → `run_metabolism` | watermark 超過 / トークン閾値（`_metabolism_token_triggered`） |
| **手動** | `SessionLifecycle.run_manual_compaction`（ペルソナメニューの「溜まった会話をあらすじにまとめる」/ Chronicle タブの生成が合流。2026-09-01 に両方とも背景ジョブ `POST /api/people/{id}/arasuji/generate` へ一本化） | ユーザーの明示操作。範囲規則は自動と同一（残す量より古い側だけ） |
| **応答前の非常畳み**（§14-3） | `run_meta_user` 冒頭 → `maybe_run_emergency_precompaction` | 話しかけた時点で高水位を**既に**超過しているイレギュラー（休眠 model の復帰等）。原因不問の回復措置で、status イベントで通知（同意は求めない） |
| **失効後の先回り畳み**（§14-4） | EventScheduler の定期見張り（10 分周期）→ `cold_precompaction_status` / `run_cold_precompaction` | 全 anchor 行が冷え切った + 提示ウィンドウが残す量と上限の**中間**を超過。編纂の総作業量は畳む時期によらず不変なので、前倒しで「冷えた再開時の定価読み」だけが消える。Chronicle 生成が有効（自律確認 ON）な persona のみ |
| **応答前の読み戻し**（§15、2026-07-30 / 再設計 2026-09-05） | `run_meta_user` 冒頭 → `maybe_run_window_refill`（**全 pulse_type**、最終防衛ラインの直前） | Pulse 開始時点で窓の**会話文が目標量（残す量）を下回っている**（水位引き上げ後の既存ペルソナ / ほぼ全編纂済みでアップデートしたペルソナ）。あらすじがどこにあるかを問わず**いちばん新しいあらすじから順に丸ごと開き**、会話文が目標量に達したら終了 — **編纂も LLM も無し**（圧縮区間に「生で見せる」印 `presented_raw` を付ける + 古い側は一次あらすじの `source_ids` から記録を合成して anchor を引き戻す）。読む範囲を字数で切る「予算」は無く、材料に読めない行があっても読める行だけで開く。実際に送る合計が上限を超えても開いた結果は保つ（WARNING のみ）。再畳みは印戻しだけで既存あらすじを再利用（`_refold_raw_view_folds` が退場計画より先に走る） |
| **発話直前の最終防衛ライン**（§15-5、2026-09-04） | `run_meta_user` 冒頭 → `ensure_window_floor`（読み戻しの直後、**全 pulse_type**） | 窓の**会話の行**が残す量を下回ったまま発話させない不変条件。読み戻しがどんな理由で埋め切れなくても、起点より古い会話を不足分だけ**生で**読み足す（あらすじの段に関係なく。覆うあらすじがあれば `presented_raw` の圧縮区間として記録）。発火は上流の失敗の印 — WARNING と context-status の `window_floor_applied_at` に残る |

範囲規則は削る側の 4 経路とも同一（残す量より古い側だけ）。読み戻しはその対称（目標量 = 残す量まで開き直す。いちばん新しいあらすじから丸ごと、字数の予算は無し。仕上げに合計を上限と比べるが超えても開いた結果は保つ）。最終防衛ラインはあらすじに依らず生で読み足す（材料があるかぎり会話の行は残す量を下回らない）。§14 の 2 経路は撤去した旧②④の復活ではない — 全量掃きせず・同意を求めず・会話開始を（非常時以外）ブロックしない（intent §14-5 の検算）。

**旧実行点2つは 2026-07-29（intent §13）で撤去された**: ①会話前（anchor TTL 失効時の `runtime_context.py` Case 3 での全量編纂 + 最小ロード）②セッションクローズ（gold_panning からの前倒し全量編纂）。どちらも予算超過と無関係に編纂を発火させ、「発火はたまに・まとめて」（intent §3-2）に反していた。過去に「会話前経路は grep で見落とされ続けた」経緯があるため記録しておく — 現在は `generate_chronicle` の直接呼び出しは自動経路には存在しない。

### 発火時にやること

1. 全 Section に `capture(live_state)` を走らせて**短期記憶（head snapshot）を再構築**
2. 同時に**長期記憶への結晶化**（履歴圧縮・Chronicle 化・Fragment 生成）を束ねて実行
3. **新しい Session を開始する**

`resolve_metabolism_anchor` のフォールバック順（intent §14-2、2026-07-29）: 当該モデルの anchor 行（**温かければ絶対に動かさない**。冷え切っていて編纂の最前線より後ろなら、最前線まで前進して永続化 — 編纂なし・LLM なしの行更新のみ。ただし**前進先はスルースのパンマーカーの次で頭打ち**にし、マーカーが読めなければ前進しない。押し出される記憶は必ずスルースを通る、が起点を動かす全経路の不変条件〈2026-08-23〉）→ 行が無ければ最前線（Chronicle の `source_ids` から導出。行は LLM 成功後の touch が立てる）→ 最前線より先の他モデル行があれば借用（編纂なしで前進する設計の persona 等）→ どれも無ければブートストラップ最小ロード。**実装済**。

**編纂の最前線** = 「どこまで編纂が終わっているか」の境界。真実は Chronicle 自身（一次エントリの `source_ids`）が持ち、anchor とは独立した persona 単位の概念（`get_frontier_anchor_id`）。anchor 行が全部消えても最前線は編纂結果と一緒に生き残る。

> ⚠️ **短期 → 長期の選別（要整理）**: 短期記憶に流入する情報がすべて長期記憶に残るべきとは限らない。特に**システム通知**（入室・アイテム増減など）は「その場で分かればいい」情報。現状は Chronicle 生成時に除外しているが、そもそも長期記憶（生ログ）側に渡さない入口選別の方が綺麗（→ [issue](../issues/short_term_to_long_term_memory_filtering.md)）。

## Anchor（節目のマーカー）

Metabolism の起点を指すマーカー。

- anchor は **`session_anchor` テーブル**（1 行 = 1 (persona, model)、列 = `ANCHOR_MESSAGE_ID / TTL_SECONDS / UPDATED_AT`）に持つ（§6-3a、2026-07-17。旧 `AI.METABOLISM_ANCHORS` 単一 JSON 列は backfill の変換元としてのみ残存）
- `UPDATED_AT` は prompt cache write 時刻で、LLM コール成功後に `touch_anchor_after_llm_call` で touch される。記帳先は **usage.model（実際に応答した model）**、touch する anchor は prefix 組成時の値を `state["_prefix_anchor_id"]` で call-local に運ぶ（persona 属性経由は廃止 — §6-5）
- `UPDATED_AT + ttl < now` で TTL 切れ = **キャッシュが冷えた**という温度情報。keep-alive / 見張り / スルース (sluice、旧 gold_panning) の defer 判定が読む。**勝手に提示範囲を縮めない**（§13 — 旧「TTL 切れ → 次の context 構築で Metabolism trigger」は撤去）。ただし冷え切った後は保守作業の解禁条件になる（§14 — 冷えた anchor の最前線への前進・先回り畳み。判定式は `_anchor_entry_is_hot` の一枚）
- **二層分離（§6-5、2026-07-17）**: 編纂（Chronicle 生成）は persona に一度（実行台帳の冪等 claim `metabolism.run`）、退役（anchor 前進）は model ごと。**退役は編纂の成功（status ok / disabled）でゲート**され、編纂失敗時は据え置き → 次回自然再試行（S2 根治）

**実装済**。

## 実装

- 発火・アンカー解決: `sea/runtime.py` / `sea/runtime_context.py` / `sea/session_lifecycle.py`（`resolve_metabolism_anchor` / `touch_anchor_after_llm_call` / `maybe_run_metabolism`）
- head 再構築: `sea/head_pipeline/integration.py`（可視化は anchor を進めた model の (persona, model) snapshot のみ — §6-5）
- 結晶化 (W4 で episode 整列に世代交代): `sai_memory/arasuji/alignment.py`（整列計画）+ `executor.py`（チャンク実行）+ `bands.py`（列のあふれ束ね）+ `entity_extractor` の相乗り。冪等 claim は実行台帳（`saiverse/execution_ledger.py`）。詳細は [Chronicle](chronicle.md)
- 退場の計画: `sea/eviction_plan.py`（純関数 `plan_eviction` — 残す量より古い側を、古い順に U ずつ刻んで全部畳む。切り位置は pulse 関節に寄せる。**エピソードに畳みを止める権利は無く**、末尾の U 未満の端数は次回へ残す。旧 episode 単位・二段構えは 2026-07-28 世代交代 — intent [`arasuji_levels.md`](../intent/arasuji_levels.md) §4）
- 提示コンテキストの圧縮区間と提示: `sea/session_window.py`（`SessionWindow` = anchor + 生ログ + 提示、`apply_folds` が digest 置き換え。`presented_raw` 印付きの区間は生のまま通す）。圧縮区間は `session_anchor.FOLDED_RANGES_JSON` に (persona, model) 単位で持つ
- 読み戻しの部品: `sea/window_refill.py`（純関数 `openable_folds_newest_first` / `merge_refill_fold`。開くループ本体は `sea/session_lifecycle.py` の `_plan_window_refill` — intent [`arasuji_levels.md`](../intent/arasuji_levels.md) §15）
- 編纂範囲: 「今回退場させる範囲そのもの」（`generate_chronicle(compile_groups=...)`）。退場する集合と編纂する集合を一致させることが、下限「退場したものは必ず編纂されている」の手続き上の保証
- Anchor 状態: `session_anchor` テーブル（1 行 = 1 (persona, model)）

## 関連概念

- [Session](session.md) — Metabolism が区切り直す対象
- [head](session.md) — Metabolism が再構築する安定領域
- [Chronicle](chronicle.md) / [Memopedia](memopedia.md) — 長期結晶化の中身
- [line / aspect](line.md) — scope が結晶化対象かどうかに効く

## 参照

- intent: [`cache_lifecycle_control.md`](../intent/cache_lifecycle_control.md)
- 地図: [`landscape.md`](../overview/landscape.md) §6
