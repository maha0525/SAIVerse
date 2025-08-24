# SAIVerse メモリ取り込み・大工事ふりかえり（2025-08-24）

## 概要

- 症状: scripts/ingest_past_logs.py 実行後、ナレッジグラフ/LanceDB に一切登録されない
（total_rows=0）。
- 影響: 取り込み結果が検索/参照できず、解析パイプラインも二次障害（エラー・空応答）を誘発。
- 結論: 保存先パスの固定/キャッシュ/非同期接続/挿入ロジックの複合要因。複数の“落とし穴”が重なっ
ていた。

## 起きていたこと（症状のログ）

- LLM応答の空振りや ValueError: empty content（初期）
- database is locked（重複 cognify 同時実行）
- IndexError: list index out of range（ベクトル数 != データ点数）
- FileNotFoundError: text_*.txt not found（DB行が消えた実ファイル名を参照）
- LanceDB が常に total_rows=0（実は 1件も挿入されていない）

## 根本原因（複合）

1. 保存先の“場所ズレ”とキャッシュ固着

- Cognee はデフォルトで site-packages/cognee/.cognee_system を基準にする実装。
- base_config/vector_config/relational_config が @lru_cache で固定化され、環境変数での上書きが
反映されない。
- vector は lancedb.connect_async() 経路で site-packages を見続けていた（同期接続だけリダイレク
トしても不十分）。

2. ベクタ挿入フィルタの不備（IndexSchema 非対応）

- LanceDB 挿入時の安全フィルタが DataPoint のテキストのみ対象にしており、IndexSchema(id, text)
（EdgeType/Document/ Summary の各インデックス）を見落としていた。
- その結果、「抽出0件＝何も挿入しない」を誘発し、常に0件のまま。

3. .txt の二重保存によるファイル名ズレ

- save_data_to_file で text_.txt を作成後、TextLoader が再保存して別名へ→raw_data_location と
実ファイル名がズレることがある。
- 手動削除後の再実行時に FileNotFoundError が顕在化。

4. 同時 cognify による SQLite ロック

- 取り込みループ内の自動 cognify と --wait-cognify の競合で database is locked。

## 実施した対策（コード・振る舞い）

1. 保存先固定とキャッシュ再評価（integrations/cognee_memory.py）

- SYSTEM_ROOT_DIRECTORY / DATA_ROOT_DIRECTORY を常に ~/.saiverse/personas/<persona>/
cognee_system へ強制。
- Cognee import 直前にキャッシュをクリア:
    - get_base_config().cache_clear()
    - get_vectordb_config().cache_clear()
    - get_relational_config().cache_clear()
    - create_vector_engine().cache_clear()
    - create_relational_engine().cache_clear()
- lancedb.connect と lancedb.connect_async の双方を“既定のcogneeパス”検出で persona 先にリダイ
レクト。

2. LanceDB 挿入フィルタの恒久化

- DataPoint だけでなく IndexSchema(id, text) の .text も確実に拾う。
- テキスト抽出0件のときは“何もせず”ではなく元の create_data_points() にフォールバック。
- ベクトル数≠データ点数の場合も縮約して挿入（ズレで全落ちしない）。

3. .txt の二重保存抑止

- 既存の text_*.txt はローダーで再保存せず、そのまま使用（file:// URI をファイルパスに正規化し
て復用）。
- ingest_data 内部参照も差し替え、確実にパススルーが効くように。

4. 欠損ファイル参照の自動清掃

- 手動削除後の DB/FS 整合性崩れを自動復旧。

5. 実運用フローの是正

- バルク取り込み時は SAIVERSE_COGNEE_AUTOCG=0 で自動 cognify を停止、最後に --wait-cognify。
- LLM モデルは gemini/gemini-2.0-flash（-lite は非推奨）を安定運用に固定。

## 検証手順（短縮版）

- バックエンド確認
    - python scripts/check_cognee_env.py --persona-id eris_city_a --print-backend --print-rel
    - 期待: vector_config.url / relational.db_path ともに persona 配下
- 最小挿入
    - python scripts/check_cognee_env.py --persona-id eris_city_a --probe-add --print-lancedb
    - 期待: LanceDB(after) で DocumentChunk_text/EdgeType_relationship_name 等が >0

## 再発防止のチェックリスト

- [ ] 取り込み前に --print-backend --print-rel で保存先が persona 配下か確認
- [ ] limit 小さめ（50〜200）で段階的に実行、--wait-cognify --verify-lance
- [ ] .env: LLM_PROVIDER=gemini, GEMINI_*_API_KEY, SAIVERSE_COGNEE_GEMINI_MODEL=gemini/
gemini-2.0-flash
- [ ] バルク時 SAIVERSE_COGNEE_AUTOCG=0、終了時のみ --wait-cognify
- [ ] LanceDB 0件が続く場合、check_cognee_env.py --try-recall --scan-lancedb で実体パス/ヒット
状況を診断

## 学び（落とし穴）

- キャッシュ（@lru_cache）に要注意: 環境変数・設定は“import と評価タイミング”に強く依存。強制
cache_clear を設計に組み込むこと。
- 非同期接続の見落とし: connect_async() の経路も必ず同様に制御。
- “安全フィルタ”の影響範囲: 入力型の拡張（IndexSchema）に追随しないと“0件”という静かな不具合に
なりやすい。
- FS/DBの整合性: 手動削除に備えて、prune のガードを pipeline の入り口に。

## 今後の改善（任意）

- 検査スクリプトの秘匿情報マスキング（APIキー表示抑止）
- 旧DB（site-packages配下）の内容を persona 配下へ移行するユーティリティ（必要であれば作成）
- パイプライン単位のヘルスチェック（各コレクションの件数/最終更新・簡易ダッシュボード）

## 参考コマンド

- 取り込み（例）
    - export SAIVERSE_COGNEE_AUTOCG=0
    - python scripts/ingest_past_logs.py --persona-id eris_city_a --file ~/.saiverse/
personas/eris_city_a/log_eris_chatgpt_001.json --conv-id chatgpt_001 --start 1 --limit 200
--wait-cognify --verify-lance --quiet
- 環境変数（例）
    - LLM_PROVIDER=gemini
    - GEMINI_FREE_API_KEY or GEMINI_API_KEY
    - SAIVERSE_COGNEE_GEMINI_MODEL=gemini/gemini-2.0-flash
    - SAIVERSE_COGNEE_GEMINI_EMBED_MODEL=gemini/text-embedding-004
    - SAIVERSE_COGNEE_GEMINI_EMBED_DIM=768

## 最終結果

- ベクタ/リレーショナルともに persona 配下へ完全固定。
- 最小挿入で LanceDB が 0→406（内訳: DocumentChunk_text=1, EdgeType_relationship_name=401 等）
に増加。
- 取り込みパイプラインが正常動作へ回復。