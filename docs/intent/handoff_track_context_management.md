# 引継ぎ: タグベースコンテキスト管理の破綻と再設計検討

**ステータス**: **解決済 (2026-04-29)** — 設計議論を経て Intent A v0.14 / Intent B v0.11 に吸収。本ドキュメントは検討経緯の記録として残す。Phase 0 タスク (P0-1〜P0-7) の出発点として参照される
**作成**: 2026-04-28
**解決経緯**:
- 2026-04-29 セッションで対話的に設計詰め (ライン 3 軸独立化 / 7 層ストレージモデル / メタ判断分岐フロー / メタ判断ログ独立領域)
- Intent A v0.14 (`persona_cognitive_model.md`) で概念整理
- Intent B v0.11 (`persona_action_tracks.md`) で実装方針 (テーブル設計 / Spell loop 保存方針 / action 文ペア保存 / handoff 3 経路修正方針)
- 詳細は両 Intent ドキュメントの「v0.14 / v0.11 で確定した事項」セクションを参照

**前提**: `persona_cognitive_model.md` v0.13 / `persona_action_tracks.md` v0.10 (本ドキュメント作成時点。解決後は v0.14 / v0.11)
**契機**: Phase C-3b 実装後の動作確認で track_autonomous の SAIMemory 多重記録問題が顕在化、根本原因はタグベースのコンテキスト管理機構と Track 機構の不整合と判明

---

## サマリ (1 段落)

SAIMemory のメッセージは現状 `tags=["conversation"|"internal"|...]` で「会話履歴かどうか」を区別しているが、Track 機構が成立すると **「同じ Track 内の前 Pulse 内容を次 Pulse で見たい」** という新しい要件が出てくる。internal タグでは context_profile の `include_internal=False` で除外されるため Track の連続性が壊れ、conversation タグに変えると user_conversation Track の会話履歴と混ざる。タグベースの 2 値分類では Track 機構を扱えない。

---

## 何が起きているか (具体的問題)

### Phase C-3b 実装後の動作確認で観察された多重記録

`speak: false` の LLM ノード (`track_autonomous.json` の `main_line_judgment`) で spell が動くと、**3 経路で同時に SAIMemory 記録が走る**:

#### 経路 A: spell loop 内の各 round (`sea/runtime_llm.py:430-442`)
```python
runtime._store_memory(persona, assistant_content, role="assistant", tags=["conversation"], ...)
runtime._store_memory(persona, combined_results, role="system", tags=["conversation", "spell"], ...)
```
- ハードコードで `tags=["conversation"]` 固定
- ノードの `memorize.tags` 設定を無視

#### 経路 B: spell loop 終了後 `_emit_say` (`sea/runtime_llm.py:980`)
```python
runtime._emit_say(persona, eff_bid, _spell_bubble2, ...)
```
- `_spell_bubble2` = details block + 最後の独白の連結 (UI 表示用)
- `emit_say` は Building history + SAIMemory に「発話」として保存
- **`speak: false` でも spell が動けば走る**
- 内部処理用ノードからもペルソナの「発話」として外向きに流入

#### 経路 C: LLM ノード本体の memorize (`sea/runtime_llm.py:1717-1768`)
```python
if prompt:
    runtime._store_memory(persona, prompt, role="user", tags=memorize_tags, ...)
if text:
    runtime._store_memory(persona, text, role="assistant", tags=memorize_tags, ...)
```
- prompt (= action template、Pulse 開始時のノード指示文) を user role で保存
- ペルソナの「発言記録」に「自分への指示」が user メッセージとして混入

### 根本問題: タグベースの限界

修正方針として最初に検討したのは:
- (1) 経路 A の tags ハードコード → `memorize.tags` 継承
- (2) `speak: false` 時は経路 B 全体を skip
- (3) 経路 C に `record_prompt: false` オプション追加

しかし**ここで根本問題に到達**:

**internal タグにすると Pulse 跨ぎで内容が見えない** (`sea/runtime_context.py:365-367`):
```python
required_tags = ["conversation", "event_message"]
if reqs.include_internal:
    required_tags.append("internal")
```
`conversation` context_profile は `include_internal=False` (`sea/playbook_models.py:498`)。

→ Track 内で複数 Pulse を連続実行する時、前 Pulse の内容が次 Pulse の base_msgs に流入しない → **Track 連続性が成立しない**。

逆に conversation タグにすると、user_conversation Track の会話履歴と autonomous Track の内部独白が**同じスレッドに混在**する。

**タグの 2 値分類 (会話 / 内部) では Track の隔離 + 連続性を両立できない**。

---

## 別セッションで設計検討すべき課題

### 1. Track 隔離 + Track 内連続性の両立

要件:
- 同じ Track の前 Pulse 内容は次 Pulse の base_msgs で見える
- 別 Track の内容は混入しない
- 対ユーザー Track と自律 Track が同じペルソナでも独立した履歴を持つ

候補アプローチ:
- **(A) Track ID ベースのフィルタリング**: SAIMemory メッセージに track_id メタデータを持たせ、context_profile が「現 Track の track_id を含むメッセージ」のみ取得
- **(B) SAIMemory スレッド分離**: まはー前回指摘 (Phase C-2 動作確認時の「サブラインとメインラインが同じスレッドに記録されるのは設計上良くない」) と整合。Track ごとに別 thread
- **(C) ハイブリッド**: グローバル会話 (対ユーザー) は共通スレッド、自律行動は Track 専用スレッド

### 2. 内部処理と外向け発話の区別

タグでの区別を辞めるとして、何で区別するか:
- ノードの `speak` 属性で区別 (speak: true は発話、speak: false は内部処理)
- Track 種別で区別 (`output_target` 属性、Intent A v0.9 で確定済み)
- メッセージ種別を明示するフィールド追加 (例: `message_kind: "utterance"|"prompt"|"thought"`)

### 3. スペル実行ログの記録方針

現状 spell loop 内で 3 種類記録される:
- assistant (spell 含む応答テキスト)
- system (combined_results、spell 結果)
- _spell_bubble2 (details block 含む UI 用)

これらを:
- 全部 SAIMemory に残すか
- pulse_logs DB のみに残し、SAIMemory には残さないか
- Track 連続性のために何が必要かに応じて選別

### 4. action template の扱い

LLM ノードの `action` (Pulse 開始時の指示文) は本来「ノードへの指示」であってペルソナの記憶ではない。
- SAIMemory に保存しない (一時情報)
- もしくは別レイヤー (pulse_logs) のみに記録
- Track コンテキスト注入機構 (Phase C-2d で導入) と同じ位置づけにする?

---

## 関連ファイル + コード位置

### 多重記録の発生箇所
- `sea/runtime_llm.py:430-442` — spell loop 内 memorize (経路 A、conversation タグ固定)
- `sea/runtime_llm.py:980` — `_emit_say` (経路 B、speak:false 無視)
- `sea/runtime_llm.py:1717-1768` — LLM ノード本体 memorize (経路 C、prompt + text 保存)

### タグフィルタの実装
- `sea/runtime_context.py:365-367` — `required_tags` 構築 (include_internal で internal タグ判定)
- `sea/playbook_models.py:498` — `conversation` context_profile が `include_internal=False`

### SAIMemory メッセージ保存
- `sea/runtime.py::_store_memory` (line 1154 周辺) — タグ付き保存の入口
- `sea/runtime_emitters.py::emit_say` (line 86) — Building history + SAIMemory 同時保存

### Track 関連
- `saiverse/track_handlers/` — Phase C-1/C-2d/C-3a で実装済みの Handler 群
- `saiverse/pulse_scheduler.py` — Phase C-3b 実装の SubLineScheduler

### Intent ドキュメント (v0.13/v0.10)
- `docs/intent/persona_cognitive_model.md` v0.13 — Pulse 階層・7 制御点
- `docs/intent/persona_action_tracks.md` v0.10 — Handler 拡張・スケジューラ責務分離

---

## 動作確認で得られた事実 (参考データ)

### 動いた部分
- ペルソナがメインライン経由で `/spell track_create activate=True` 発火 → 自律 Track 起動 ✓
- SubLineScheduler が track_autonomous を回す ✓
- ペルソナが note_create / note_open / track_complete を自律的に発火 ✓
- `track_complete` 後に SubLineScheduler が次 Pulse を起動しなかった ✓
- ユーザー発話で track_user_conversation に切り替え + Track コンテキスト注入 ✓

### 課題として残った部分
- 上記 3 経路の多重記録 → 本ドキュメントの主題
- ペルソナが `note_create` の戻り値を待たずに `note_open(note_id="new_created_note_id")` を placeholder で発火 (= 同 round 並列実行制約への理解不足)
  - 暫定対処案: `note_create` に `open=True` オプション追加 (track_create.activate=True と同パターン)

### 動作確認時のログ抜粋 (タイムライン)
```
22:52:51  [autonomous_pulse 開始]
22:53:30  spell round: note_create + note_open(placeholder) 発火
22:53:30  spell result: note_create 成功 / note_open エラー
22:53:37  spell round: note_open(正しい id) 発火
22:53:37  spell result: 成功
22:53:50  spell round: 教訓書き込み + track_complete 発火
22:53:50  spell result: completed
22:53:58  [LLM ノード本体 memorize] action template が user/internal/autonomous_pulse で保存
22:54:18  ユーザー発話 → 対ユーザー Track へ切替
22:54:22  Track コンテキスト注入 (新形式 /spell ...) ← Phase C-2d/C-3a 機構が機能
22:54:28  メインライン応答
```

---

## 引継ぎ時の優先順序 (案)

1. **設計議論**: タグベース管理の代替案を確定 (上記 1-4 の論点)
2. **Intent 改訂**: 確定内容を `persona_cognitive_model.md` v0.14 / `persona_action_tracks.md` v0.11 に反映
3. **実装**: 影響範囲は広い。SAIMemory のスレッド分離が必要なら大規模改修
4. **既存ペルソナのデータ移行**: Track ID メタデータ追加 / スレッド分離による履歴整理

---

## 引継ぎ時の判断材料: 規模と緊急度

- 規模: **大** (SAIMemory レイヤー + sea/runtime_llm.py + context_profile + 既存ペルソナデータ移行)
- 緊急度: **高** (これを直さないと自律 Track の連続稼働が成立しない、Phase C-3 の core 機能が機能不全)
- ただし現状でも単発の自律 Pulse は動く (今回の動作確認で実証済み)。**「Track 内で複数 Pulse 連続実行して意味のある作業を進める」フェーズに入る前**にこの再設計を完遂する必要がある。
