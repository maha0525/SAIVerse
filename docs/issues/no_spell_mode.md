# Issue: Spell 起動を一切行えないモードの整備 (ペルソナへのモード通知含む)

**ステータス**: 後継 intent に集約（スペル不使用モードは v0.3.14 で発行済み。旧案は履歴）
**優先度**: low
**作成日**: 2026-05-08
**現行の正典**: [スペル不使用モード](../intent/spell_disabled_mode.md)

## 現在の扱い（2026-10-02 確認）

ここで求めた「スペルを使わないモード」と「使えないことをペルソナに伝える仕組み」は、
後継の [スペル不使用モード](../intent/spell_disabled_mode.md) に設計・実装・検証記録が集約された。
同 intent のステータスは完了で、[release_history](../overview/release_history.md) の
2026-09-22（v0.3.14）発行記録に、まはーによるスペル不使用時のトークン減の実測がある。
この issue の旧案 A〜C を新たに実装する必要はない。

現行の入口はペルソナ設定の **スペル不使用モード（`AI.SPELL_ENABLED=false`）**。
新しい Track 種別や Playbook ノードの無効化フラグを足す設計ではない。

- [`SpellListSection`](../../sea/head_pipeline/sections/spell_list.py) は無効時に
  「スペルは現在使用できません。/spell コマンドを使用しないでください。」を表示し、
  切り替え時の通知も担う。能力説明の出し分けと保存時の反映は後継 intent §4 に従う。
- 実行側は既存の spell loop / realtime spell の判定に加え、
  [`lg_llm_node`](../../sea/runtime_llm.py) の `pre_spells` も無効時にスキップし、
  警告ログと status イベントで伝える（後継 intent §4-7）。
- `meta_simple_speak` と `track_user_conversation` の JSON 自体は現在も存在する。
  ただし、旧案の Playbook 選択とペルソナ単位の不使用モードを同一視しない。
  モードの対象外・反映範囲・残る制約は後継 intent §5–6 に従う。

**今回の検証範囲**: 隔離環境・LLM なしで
[`test_spell_disabled_mode.py`](../../tests/test_spell_disabled_mode.py) と
[`test_head_pipeline_spell_list.py`](../../tests/test_head_pipeline_spell_list.py) を実行した。
今回新たに実機検証やトークン実測をしたわけではなく、発行・実測の根拠は上記の既存記録。

## 経緯: 起票時の記録（以下の解決案は後継仕様に置き換わった）

2026-10-02: 後継 intent と発行記録への参照を追加し、未着手の旧提案として再実装されない
ようにした。以下は 2026-05-08 の問題意識・代替案・関連リソースを残した履歴。

**ステータス**: 🔲 未着手
**優先度**: low
**作成日**: 2026-05-08
**関連**: `builtin_data/playbooks/public/meta_simple_speak.json`, `builtin_data/playbooks/public/track_user_conversation.json`, [docs/intent/addon_speak_hooks.md](../intent/addon_speak_hooks.md)

### 背景

現状、`meta_simple_speak` Playbook が「Spell loop を起動せず純粋に発話だけする」経路として残っている (`sub_speak` を sub_play で呼ぶだけの 1 ノード構成、`user_selectable=true`)。Phase 3 移行で旧 `meta_user` 系を整理した時に削除候補から外れていたが、その「残った理由」は明示的に議論されていなかった。

2026-05-08 の整理で以下が確認された:

- `track_user_conversation` のメインライン LLM ノードは Spell loop を回す → ペルソナの判断で `/spell` を発話に含めれば Spell が実行される
- `meta_simple_speak` は `sub_speak` 1 ノードのみ → ペルソナが `/spell` を書いても Spell loop が無いので無視される (発話としてはそのまま出力されるが)

つまり「Spell loop を起動しない最小発話モード」として独立した役割を持つ。ただし**ペルソナ側にこのモードであることが伝わらない**ため、Spell を呼ぶつもりで `/spell` を書いてしまっても誰も実行しない、という UX 上の問題がある。

### 解決案候補

#### 案 A: `meta_simple_speak` を専用モードに昇格 + ペルソナ通知

- `meta_simple_speak` を「Spell 不使用モード」として明示的に位置付ける
- ペルソナへの通知メカニズムを追加: メインライン LLM のシステムプロンプトに「現在は Spell 起動が無効です。`/spell` を書いても実行されません」を注入
- UI 側でも「Spell 不使用」モードを選択しているとペルソナに分かる表示
- `meta_simple_speak` Playbook の display_name / description を更新

#### 案 B: `track_user_conversation` に Spell 無効化フラグを追加

- メインライン LLM ノードに `disable_spell_loop: bool` を追加
- フラグ on のときは Spell loop を回さず、`<system>` タグでペルソナに状況を伝える
- メリット: Track コンテキスト注入は維持しつつ Spell だけ封じる
- デメリット: ノード仕様が増える

#### 案 C: 新 Track 種別 `track_silent_response`

- 「ペルソナが発話のみ行う Track」を新設
- `meta_simple_speak` は廃止、新 Track に置き換え
- メリット: 認知モデル (Track ベース) と整合
- デメリット: 単発発話のために Track を切るのは過剰かも

### 関連リソース

- `builtin_data/playbooks/public/meta_simple_speak.json` — 現行 Playbook
- [docs/intent/addon_speak_hooks.md](../intent/addon_speak_hooks.md) — `meta_simple_speak` 経由の TTS 動作要件 (line 230, 273, 286)
- [docs/intent/subplay_result_flow.md](../intent/archive/subplay_result_flow.md) — `sub_speak` への経路
- [docs/intent/persona_cognition/revisions.md](../intent/persona_cognition/revisions.md) v0.27, v0.28 — 旧 meta_user 系削除時に `meta_simple_speak` を残した経緯

### ログ

- 2026-05-08: issue 起票。実害 (誤動作) はないが UX 改善案件として記録。優先度 low。
