# Playbook / SEA

ペルソナの行動パターンを定義する「Playbook」と、その実行エンジン「SEA」の概要。詳しい概念は [concepts/playbook.md](../concepts/playbook.md)、作り方（正確なノードスキーマ）は [開発者ガイド: Playbook 作成](../developer-guide/creating-playbooks.md) を参照。

## 概要

**SEA (Self-Evolving Agent)** は LangGraph ベースの Playbook 実行ランタイム（`sea/runtime.py`）。**Playbook** は LLM / tool / speak ノードの有向グラフを JSON で宣言したもので、条件分岐・反復が組める。

ペルソナの1回の [Pulse](../concepts/pulse.md)（認知サイクル）は Playbook を1つ実行する。入口となる**メタ Playbook**が2系統ある:

- `meta_user` 系（`track_user_conversation.json` 等）— ユーザー入力を捌く
- `meta_auto` 系（`track_autonomous.json`、[メタ判断](../concepts/meta-judgment.md) の `meta_judgment*.json` 等）— 自律 Pulse を捌く

## ノードと配置（要点）

- ノードは `llm` / `tool` / `memorize` / `speak` / `subplay` / `set` / `exec` / `pass` など。各ノードのフィールドの正は [`sea/playbook_models.py`](../../sea/playbook_models.py)
- Playbook JSON は `builtin_data/playbooks/public/`（または `~/.saiverse/user_data/playbooks/`）に置き、**`python scripts/import_playbook.py --file <path>` で DB に取り込む**（置くだけでは反映されない）
- LLM が発話中に `/spell run_playbook name='...'` と書くと、指定 Playbook が**サブライン**として動的起動される（→ [Spell](../concepts/spell.md)）

> ⚠️ **function calling は使わない**（キャッシュを壊すため）。structured output + tool ノード固定実行が正道。詳細な設計哲学と正確なフィールド名は [開発者ガイド](../developer-guide/creating-playbooks.md) にまとめてある（このページでノードスキーマを二重管理しない）。

## 同梱 Playbook

29 本の一覧は [Playbook カタログ](../reference/playbook-catalog.md) を参照。

## 次のステップ

- [concepts/playbook.md](../concepts/playbook.md) - 概念と実装入口
- [Playbook 作成](../developer-guide/creating-playbooks.md) - 独自 Playbook の作り方
- [Playbook カタログ](../reference/playbook-catalog.md) - 同梱一覧
