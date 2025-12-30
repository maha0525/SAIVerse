# SEA integration draft

目的: SEA(Self-Evolving Agent) を SAIVerse の会話/意識ラインに組み込み、Router+Playbook 実行で応答・自律行動を駆動する。

## 差し込みポイント (option B)
- ユーザー問い合わせ: 既存の対話ハンドラから「メタ Playbook(meta_user)」を実行。
  - meta_user: router → execute_subgraph → speak
- 自律発話: ConversationManager._trigger_next_speaker() で run_pulse の代わりに「meta_auto」を実行。
  - meta_auto: router → execute_subgraph → think

## ノード規約
- speak ノード: Building history/UI/gateway に出力し、SAIMemory へ `conversation` タグ付きで記録。
- think ノード: Building には出さず、SAIMemory に pulse_id 付き internal タグのみで記録。
- Router は Playbook 名と params を返す。None を選べる。execute_subgraph が LangGraph を走らせ final_output を返す。

## LangGraph 状態の扱い
- AgentState は実行中のみ保持 (messages/inputs/context/final_output)。
- 実行終了後、必要な断片だけ既存ストレージへ書き戻す。
  - speak: building history + gateway + SAIMemory(conversation)
  - think/内部プロンプト: SAIMemory(internal, pulse_id)

## LLM / Tool ブリッジ
- LLM: `llm_clients.get_llm_client` を使う薄いラッパを用意し、LangGraph から呼べるようにする。
- Tool: Playbook action 名→`tools.TOOL_REGISTRY` を解決するマッピング層。軽量 LLM でも引数が揃うよう、必要なら事前質問を Playbook に記述する運用とする。

## Playbook 永続化
- DB テーブル `playbooks`: id, name, description, scope(enum: public/personal/building), created_by_persona_id, building_id, schema_json, nodes_json, created_at, updated_at。（models.py 追加済み）
- 既存 DB への適用例: `python database/migrate.py --db database/data/saiverse.db`
- ロード時フィルタ:
  - scope=public
  - scope=personal AND created_by_persona_id = persona
  - scope=building AND building_id == current_building (将来用)
- save_playbook ツールはファイル保存ではなく DB 書き込みに差し替える。

## 近いタスク
1) SEA ランタイムラッパ（LLM/Tool ブリッジ＋meta Playbook 実行器）の雛形を追加。
2) ConversationManager とユーザー入力ハンドラにフックを挿入するための adapter 層を用意（run_pulse の呼び換え）。
3) Playbook テーブルのスキーマ追加と migrate スクリプト案を作成。
4) 既存 Manual モードは `speak` ノードだけの meta Playbook で置き換え。
5) building スコープはカラム定義済み。利用ロジックは後続。

## 段階的ロールアウト & 検証
- Phase 1: SEA ランタイム雛形＋DB スキーマだけ追加し、既存フローは変更しない。ユニットテストで meta Playbook 実行が単体で動くことを確認。
- Phase 2: ユーザー発話経路だけを meta_user に切り替え。API/gateway 経由の対話が従来どおり出力されるか E2E で確認。
- Phase 3: 自律ループを meta_auto へ切替。ConversationManager の周回で building history が正しく更新されるか、オキュパンシーが多い場合も確認。
- Phase 3.1: LLM ノードでは tools=[] を明示し、Playbook 内の TOOL ノード経由でのみツールを使う。
- Phase 4: building スコープ対応と Playbook 保存/読み出しの権限フィルタを有効化。skip/unset の挙動や権限漏れを回帰テスト。
