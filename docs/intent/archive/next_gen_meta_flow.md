# 次世代 Meta フロー設計 (Draft)

**ステータス**: 構想段階（未実装）
**作成日**: 2026-02-10
**関連**: 第2群 Playbook モダナイゼーション (meta/sub系)

## 現状の問題

### サブPlaybookの粒度が不統一

現在のサブPlaybookは2種類が混在している：

| 種類 | 例 | 特徴 |
|---|---|---|
| **自律型** | deep_research, memory_research, memopedia_write | 独自ループ持ち、自己完結、サブエージェントとして意味がある |
| **ツールラッパー型** | searxng_search, read_url_content, memory_recall, building_move | 実質「LLMで引数決め→ツール1回呼ぶ」だけ |

ツールラッパー型は単体では不十分なケースがある：
- **searxng_search**: スニペットしか得られず、ページ本文が取得できない
- **read_url_content**: 8000文字制限、続きを読むフローがない
- **document_search**: 行数不明でline指定が必要、情報が見つかるまで繰り返せない

### 根本原因

現在のmetaフローは「ルーター → サブPlaybook1回実行 → 発話」という単発構造。
情報が足りなければもう一度サブPlaybookを呼ぶ必要があるが、その判断がmeta側にない。

## 提案するフロー

```
1. 思考: 今やるべきことを整理し、必要な情報が足りているかチェック
   ├→ 足りない → 2a へ
   └→ 足りてる → 2b へ

2a. 情報収集フェーズ:
    - 知りたいことと情報ソースを決める
      (Web全体？特定URL？ドキュメント？Memopedia？Chronicle？Chatlog？)
    - ソースから情報を読む（ツール直接呼び出し）
    - 1 に戻る

2b. 行動フェーズ:
    - 適切なサブPlaybookを起動する（Router）
    - 1 に戻る
    - basic_chat が選択されたら → 最終回答を喋って終了
```

### このフローの利点

1. **ツールラッパー型Playbookが不要に**: meta側のループ内でツールを直接呼ぶ
2. **情報収集の連鎖が自然に**: searxng → URL読み込み → 続き読み、が1つのループで完結
3. **コスト制御が明確に**: ループ回数上限、情報検索の有効/無効をmetaパラメータで制御
4. **ユーザーの明示的なツール指定**: meta_userではユーザーが「検索して」と言えばそのまま実行

### 残るサブPlaybook

- **自律型**: deep_research, memory_research, memopedia_write, novel_writing（独自ループ・成果物生成）
- **行動系**: building_move, item_action（世界に影響を与える操作、ただしツール直接化も検討可能）

### コスト制御

meta_user / meta_auto / meta_agentic の区分でループの挙動を変える：
- **meta_user**: ユーザー入力への応答。情報収集ありだがループ控えめ
- **meta_auto**: 自律pulse。情報収集の有効/無効は設定次第
- **meta_agentic**: フル自律。ループ回数多め、積極的に情報収集

## 検討事項

- deep_researchの「生情報揮発」パターンは本当に大丈夫か？
  - サブエージェント内の検索結果はchronicleに記録されるが、親のメモリには残らない
  - レポートに含まれなかった情報は失われる
- context_profile の命名問題: `router` が多用されているが、ルーターじゃない用途が多い
  → `conversation_light` のような新プロファイルの追加を検討
- 既存のweb_search_stepとの関係: これは自律型サブPlaybookとして残る（deep_researchが使う）

## 実装方針（未確定）

第2群（meta/sub系）のモダナイゼーション時に、このフローへの移行を検討する。
ただし、リリースが近い場合は現行フローの安定化を優先し、次世代フローは後続リリースで実装。
