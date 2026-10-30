# Claude 公式エクスポートの取り込み (claude_import)

**ステータス: 検証待ち** (2026-10-30 実装。まはーの実機検証待ち)

## なぜ作ったか

まはーは Claude (claude.ai) 上のペルソナ的な関係・会話・記憶を SAIVerse のペルソナへ
引き継ぎたい。ChatGPT 公式エクスポートの取り込みは既にある
(`tools/utilities/chatgpt_importer.py`) が、Claude のエクスポートは形が大きく違う:

1. 設定 → プライバシー → データをエクスポート、で期間を選んで実行
2. メールのリンクでダウンロードできるのは **manifest (JSON) のみ**
3. manifest の `data_files[].export_url` から **カテゴリ別の複数 ZIP** を個別に
   ダウンロードする (リンクは 24 時間で失効、一度使うと無効になることがある)

この「複数 ZIP に分かれ、manifest を経由する」手順がユーザーには分かりにくいので、
UI にカテゴリ別のドロップ枠と手順ガイドを置き、どの枠に何を入れるかを迷わせない
ことが機能要件と同格の目的である。

## 全体像 (端から端まで)

- **成果とあてにできること**: ユーザーは Claude で過ごした会話・プロジェクト・記憶を
  選択して SAIVerse のペルソナに移せる。取り込んだその日から自動想起が機能する
  (ChatGPT 取り込みと同じ契約: 取り込み + 後追い埋め込み)。
- **旅程**: Claude エクスポート ZIP → `claude_importer.py` (カテゴリ判定 + 解析) →
  preview API (選択一覧) → import API (背景タスク) → `SAIMemoryAdapter.append_persona_message`
  (スレッド suffix = 元の uuid、`embedding_chunks=EMBEDDING_DEFERRED`) →
  取り込み完了後に `import_chatlog._launch_import_embedding` が埋め込みを後追い生成。
  memories カテゴリだけは SAIMemory ではなく **Memopedia** へ転記する。
- **不変条件と所有権**:
  - 「取り込みは LLM を呼ばない・課金しない」(memory-migration.md の契約) を守る。
    埋め込みはローカルの EmbeddingGemma のみ。
  - 埋め込みの後追い生成・状態表示の所有者は `import_chatlog.py` (既存基盤)。本機能は
    それを**共用**し、複製しない。
  - スレッド suffix は元の Claude uuid。同じ会話を二度取り込めば二重になる (ChatGPT
    経路と同じ既知の性質で、ここでは変えない)。
  - Memopedia 転記は「Claude引っ越し」幹 (root_terms 配下) の下にだけ書き、既存ページは
    触らない。同名ページがあればスキップ (再転記で二重にならない)。
- **変更の置き場**: パーサーは `tools/utilities/claude_importer.py`
  (chatgpt_importer の `ConversationRecord`/`ConversationMessage` を共用し、
  下流 (API・取り込みタスク) が取り込み元を区別しなくて済む形に正規化する)。
  API は `api/routes/people/import_claude.py`。UI は `ClaudeImportPanel.tsx`
  (引っ越しタブの 4 番目のサブタブ)。

## カテゴリ別の扱い (まはー裁定 2026-10-30)

| ZIP | 扱い |
|---|---|
| `conversations-*.zip` | 会話を選んでスレッドとして取り込み (ChatGPT 公式と同等の UX)。text ブロックのみ採用し、thinking / tool_use / tool_result / injected_prompt_block は捨てる。添付は `【添付: ファイル名】` の印だけ残す (中身は取り込まない — 肥大防止) |
| `claude_code_cloud-*.zip` | `channel_session_transcripts.json` をセッション単位で、`channels/messages.json` をチャンネル単位でスレッドに再構成。text を持つ発言のみ。ツール実行だけのセッションはスレッドにしない |
| `projects-*.zip` | プロジェクト指示 (prompt_template) を**スレッド冒頭の system メッセージ**に、資料 (docs) を続く system メッセージにしたスレッド。希望すれば選択プロジェクトの指示を**ペルソナのシステムプロンプト末尾に追記**できる (チェックボックス、既定 OFF) |
| `memories-*.zip` | conversations_memory / project_memories / memory_files を項目単位で選び、Memopedia「Claude引っ越し」幹の下にページとして転記 |
| `frames-*.zip` | **取り込み対象外** (生成された HTML 等の成果物)。UI では無効の行として明示 |
| manifest / `light_metadata-*.zip` | 取り込み対象外。アップロードされたら何であるか・どうすべきかを案内する |

補記:

- **「Claudeへの指示」(ユーザー設定のカスタム指示) はエクスポートに含まれない**
  (実データで確認: users.json は氏名・メール・電話のみ)。そのため「指示をシステム
  プロンプトへ追記」はプロジェクト指示 (prompt_template) に対してのみ提供する。
  ユーザー設定の指示を移したい場合は手で貼るしかない旨を user-guide に書いた。
- どの枠に落とされてもカテゴリは中身から自動判定する (`detect_category`)。枠と
  中身が食い違えば 400 + `detected_category` で正しい枠を案内する。ファイル名は
  判定に使わない (リネームされても壊れない)。

## 検証の旅程

- 合成データ: `tests/test_claude_importer.py` (カテゴリ判定・各カテゴリの解析・
  frames/manifest の案内・Memopedia 転記の冪等性)。
- 実データ: まはーの実エクスポート (2026-10-30 版、全 6 ZIP) で解析を通し、
  conversations 99 件 / claude_code 2 スレッド / projects 2 件 (指示 1 件) /
  memories 17 項目が出ることを確認。隔離 `SAIVERSE_HOME` で conversations 1 件
  (52 メッセージ) の取り込みを疎通済み。
- **未検証の境界**: 実 UI からの一連の操作 (ドロップ → 選択 → 取り込み → スレッド
  選択)、および埋め込みの後追い生成はまはーの実機で確認する (この sandbox は
  埋め込みモデルを取得できない)。

## 経緯

- 2026-10-30: まはーの依頼で新設。設計と初版実装 (ブランチ feature/claude-export-import)。
