# Issue: スケジュール / チャット UI で Spell の引数を確定値で指定したい場面

**ステータス**: 🔲 未着手
**優先度**: low
**作成日**: 2026-05-08
**関連**: [docs/intent/persona_cognition/revisions.md](../intent/persona_cognition/revisions.md) v0.28-v0.29, `frontend/src/lib/preSpells.ts`

## 背景

v0.29 でスケジュール / チャット UI から Spell + Playbook を選べるようになったが、**Spell の引数は常に省略形** (`/spell name='X'`) で送られる。バックエンドで `spell_args_decider` Playbook がペルソナの認知から動的に決める。

これは多くの場面で正しい挙動だが、以下のケースでは「ユーザーが UI で確定値を渡したい」需要がある可能性:

- メール送信スペルで宛先・件名・本文を UI で確定指定したい (まはーがテンプレ的に同じ内容を毎回送る場合)
- 画像生成スペルで特定のプロンプトを毎回固定で使いたい
- スケジュールで毎日同じ引数で Spell を呼びたい

現状は `PLAYBOOK_PARAMS.pre_spells` に直接 `/spell name='X' args={...}` 形式を書けば動く (バックエンドの `_SPELL_PATTERN` が確定形をパースする) が、UI から指定する経路がない。

## 解決案候補

### 案 A: Spell 選択ドロップダウンに「引数編集」モード追加

- Spell をチェックすると、その下に各引数の入力フィールドが展開
- 引数 schema は `/api/people/spells` でレスポンスに含めて取得
- 確定値で指定するか省略 (ペルソナに委ねる) かを選択可能
- UX: 「使うスペル」リストから 1 つチェック → 各引数を編集 or 「自動」のまま

### 案 B: Spell 詳細編集モーダル

- Spell をクリックすると別モーダルで引数編集
- 複数 Spell の引数を同時に編集できる
- UX: 候補リスト → 選択 → 引数編集モーダル → OK で確定

### 案 C: 高度モードで JSON 直接編集

- 通常モードはチェックボックスのみ (現状と同じ)
- 「高度」スイッチで `pre_spells` の JSON を直接編集できる UI
- 上級ユーザー向け、UX 簡素化

実装難度: A < C < B
UX の良さ: A or B > C

## 関連リソース

- `frontend/src/lib/preSpells.ts` — `parsePreSpellsForUI` / `buildPreSpellsFromUI` 共通ユーティリティ。引数あり形式の対応もここで一元化できる
- `frontend/src/components/ScheduleModal.tsx` — スケジュール UI
- `frontend/src/components/ToolModeSelector.tsx` — チャット UI
- `api/routes/people/summon.py` — `/api/people/spells` エンドポイント (parameters 情報を含めて返す形に拡張可能)
- [docs/intent/persona_cognition/revisions.md](../intent/persona_cognition/revisions.md) v0.28 — 引数あり Spell の `_SPELL_PATTERN` 仕様

## ログ

- 2026-05-08: issue 起票。現状動作に問題なし、UX 拡張案件として記録。優先度 low。
