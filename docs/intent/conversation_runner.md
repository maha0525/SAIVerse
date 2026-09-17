# Intent: 会話シナリオランナー (`scripts/run_conversation.py`)

- **Status**: v0.1 (2026-07-06 起草)
- **Owner**: まはー / 主利用者はエア (AI エージェント)
- **関連**: `docs/intent/sandbox_world_clone.md`, `saiverse/day_scenario.py`
  (RealConversationUserEventDriver / SyncJudgmentDispatcher),
  `docs/intent/agent_inspection_cli.md`

## 1. なぜ作るか

「応答がおかしい」系の再現・修正確認は、これまでまはーがチャット UI に張り付いて
手で打つしかなかった。台本 (複数ターンのユーザー発話) をサンドボックス世界の
ペルソナへ実チャット経路で流し、transcript を得る軽量ランナーがあれば、
プロンプト/playbook 改修の前後比較をエージェントが自走できる。

一日シミュレータとの違い: 仮想クロック・時間割・判断点を持たない。
「会話だけ」を最小の段取りで回す道具。

## 2. 不変条件 (INVARIANTS)

1. **本番に向けない (ハード拒否)**。会話テストは偽の記憶をペルソナの memory.db に
   committed する = 記憶汚染。この実行が書く場所 — DB パス・SAIVERSE_HOME
   (memory.db と建物ログの置き場)・SAIVERSE_USER_DATA_DIR・transcript の出力先 —
   のどれかが本番 (`~/.saiverse` 配下) を指す場合は起動を拒否する。
   **本番の場所は `Path.home() / ".saiverse"` で固定し、SAIVERSE_HOME からは導かない**。
   SAIVERSE_HOME はテスト環境を指すために書き換える変数なので、それを本番の基準に
   すると、テスト環境を指した瞬間に番人が本番を見張らなくなる (§5)。
   上書きフラグは意図的に用意しない (向けたいユースケースが存在しない)。
   判定は一日シム・世界複製・ペルソナ複製と共有の `scripts/_shared/production_guard.py`。
2. **実チャット経路を通す**。building_messages 記録 → user_conversation Track
   activate → main_line Pulse (auto_ingest 含む) という
   `RealConversationUserEventDriver` の正規経路をそのまま使う。ショートカット
   (LLM 直叩き等) は再現性を壊すので作らない。
3. **transcript は実在記録から組む**。ペルソナ応答は building_messages の増分
   (連番高水位比較) から抽出する。応答ゼロは「(応答なし)」と正直に出す。
4. **環境切替の既定はテスト環境**。SAIVERSE_HOME / SAIVERSE_USER_DATA_DIR が
   未設定なら `test_data/` を指すよう起動時に自前で設定する (saiverse モジュール
   import 前)。呼び出し側の env 切替忘れ = 本番向け事故、を構造的に防ぐ。

## 3. 台本形式

```json
{
  "persona_id": "quon_city_a",
  "title": "挨拶と近況",
  "messages": [
    "クオン、おはよう",
    "昨日は何をしてたの？"
  ],
  "leave": true
}
```

- `messages`: ユーザー発話の列。各発話ごとに Pulse が同期実行され、応答を待って
  次へ進む
- `leave`: true なら最後に退室 (user_conversation Track を pending へ)。
  会話終了判断 (post_conversation) は撃たない — それは一日シムの領分

CLI から `--message` の繰り返しでも台本なしで実行できる。

## 4. スコープ外 (non-goals)

- 会話終了判断・欲求の植え付け等の認知モデル操作 (一日シムを使う)
- 応答品質の自動採点 (transcript を出すまで。評価はエージェント/まはーが読む)
- HTTP (API サーバー) 経由の実行 — API 層のテストは `test_fixtures/test_api.py` の
  領分。本ランナーは in-process で回す (サーバー起動不要・同期・低摩擦)

## 5. 経緯

- **2026-09-17**: 隔離環境での E2E 実行中に、§2-1 の番人が SAIVERSE_HOME を本番の
  基準にしていたと分かった。SAIVERSE_HOME をテスト環境へ向けた状態 (テスト環境を
  使うときの普通の形) で `--db-file` に `~/.saiverse` 配下の DB を渡すと、拒否されずに
  通る作りだった。本番の場所を `~/.saiverse` に固定し、SAIVERSE_HOME /
  SAIVERSE_USER_DATA_DIR / 出力先も検査対象に加えた。同じ判定を、番人を持って
  いなかった一日シム (`scripts/run_day_sim.py`) と、複製先が本番の中かを見ていなかった
  世界複製・ペルソナ複製にも入れた。
