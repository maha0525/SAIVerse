# 想起用埋め込みの EmbeddingGemma 2 への移行

**ステータス: 設計中 (2026-10-12 起草)**

## これは何か

SAIVerse の想起 (記憶の検索) は、文章を数値の列 (ベクトル) に変換して「意味が近いか」を測っている。この埋め込みモデルを multilingual-e5-small (384 次元) から **EmbeddingGemma 2 の 4bit 量子化 ONNX (テキスト部、768 次元)** に置き換える。利用者から見える変化は三つ: 日本語の想起の精度が上がる、必要なダウンロードと常駐メモリが今より小さくなる、そして将来、画像・音声を文字と同じ埋め込みモデルで想起できる土台ができる。

e5 と新モデルを選べるようにはしない (2026-10-12 まはー裁定「切り替え不要」)。全ユーザーが新しい埋め込みモデルに移り、旧実装 (e5 と fastembed を使うコードと依存) は削除する。問題が出たら git revert で戻す。

## 決定の根拠 (計測 2026-10-12)

計測一式は `experiments/embeddinggemma2_bench/results.md` (ブランチ experiment/embeddinggemma2-bench)。採用する構成 = ONNX 4bit 量子化 (`onnx-community/embeddinggemma-2-ONNX` の `model_q4`、revision 固定) の対 e5-small:

| | e5-small (現行) | EmbeddingGemma 2 (q4 ONNX) |
|---|---|---|
| ダウンロード (モデル本体) | 470MB | **174MB + 32MB (tokenizer)** |
| 常駐メモリ (ロード後 RSS) | 805MB | **223MB** |
| 想起クエリ 1 本 (CPU) | 6.2ms | 29.8ms |
| JSQuAD Recall@5 / Recall@1 | 0.9563 / **0.8431** | **0.9579** / 0.8330 |
| JSTS Spearman (対称プロンプトの最良値) | 0.7892 | **0.8553** |

数字の読み方の注意 (ベンチの正直な範囲):

- 常駐メモリの差が出るのは待機〜クエリ時。**全件再計算のような一括処理の最中はどちらも 2.2〜2.5GB 使う** (ピークはほぼ同等)。「メモリ半分以下」は常駐時の話。
- e5 の 470MB は配布物のうちモデル本体 (`onnx/model.onnx`) の値。実際の e5 配布一式には別の ONNX 変種も含まれ、これより大きい。
- JSTS の 0.8553 は STS 用プロンプト同士の値。本移行で採用するクエリ用・文書用プロンプトでの値は 0.8372 / 0.8210 で、どちらでも e5 (0.7892) を上回る。
- 上位 5 件の正解率 (Recall@5) は上回るが、**上位 1 件の正解率 (Recall@1) と順位の平均 (MRR@10) は僅かに下回る** (-0.010 / -0.005)。
- 量子化していない元のモデルとのベクトルのずれは、日本語 30 本の照合で cosine 0.973〜0.993。ベンチの精度にはほぼ現れなかった。
- 全ベクトルの一括再計算は 8.4 texts/s (CPU)。1 万チャンクで約 20 分。e5 (33.9 texts/s) の約 4 倍かかる — **会話ログのインポートの所要も同じ比で伸びる。**
- 計測は 1 台 (Ryzen AI 9 HX 370、CPU のみ) での値。

## 不変条件

1. **利用者の記憶は一つも失われない。** ベクトルは原文からいつでも作り直せるデータで、移行で変更するのはベクトルだけ。原文 (メッセージ・Chronicle・Memopedia・Fragment) には触れない。
2. **移行中も会話と記憶の保存は止まらない。** 弱まってよいのは想起の効きだけで、それも再計算の完了とともに全面回復する。
3. **移行後、ベクトルを持つ全 4 系統 (メッセージ / Chronicle / Memopedia ページ / Fragment) が新しい埋め込みモデルのベクトルで揃う。** 古いモデルのベクトルが残ると、その記憶は**エラーにならず黙って想起から消える** (下の「現状の事実」)。この静かな欠落を残したままにしない。
4. **依存を増やさない。** onnxruntime (導入済み) と tokenizers (fastembed 経由で導入済み) で動かす。torch / sentence-transformers は持ち込まない。fastembed は役目を終えて外す。

## 現状の事実 (調査 2026-10-12)

調査の全文は調査書 ([docs/handoff/2026-10-12_embedding_consumer_survey.md](../handoff/2026-10-12_embedding_consumer_survey.md)) に、各事実の ファイル:行 がある。設計が立脚する事実だけ挙げる:

- ベクトルは各ペルソナの memory.db の 4 テーブル (`message_embeddings` / `arasuji_embeddings` / `memopedia_embeddings` / `memopedia_fragment_embeddings`) に JSON 文字列で入っている。**次元の記録はどこにも無い。**
- 次元が合わないベクトルの扱いは経路ごとに違う: メッセージ検索 (semantic recall) では行ごとに WARNING を出してその行を飛ばし、統合検索 (unified recall) ではログ無しで飛ばす。自動想起では埋め込みのヒットが無い記憶を採用しないためほぼ全滅し (残るのはタイトル一致のトリガーだけ)、鮮明想起では類似度 0.0 として候補に混ざる。つまり**混在はエラーではなく「記憶が黙って見つからない」の形で現れる**。
- 既存の再計算 (起動時の警告バナー / API / CLI) が作り直すのは**メッセージのベクトルだけ**。Chronicle / ページ / Fragment には「ベクトルがまだ無い行のぶんを生成する」機構 (Metabolism ごとの `ensure_recall_embeddings`) しか無く、**古い次元の行を再計算する機能が存在しない**。
- 起動時のモデル変更の検出が見るのは、モデル名の文字列と `message_embeddings` だけ。再計算が終わっても警告の表示は再起動まで消えない。CLI で再計算した場合は記録 (`embed_metadata.embed_model`) が更新されない。なお `embed_metadata` テーブルは埋め込み専用ではなく、他機構の印も同居している — 触るのは `embed_model` キーだけに限る。
- 自動想起のしきい値には**出自の違う二組**がある。0.86 (env) と message +0.02 (env) は、e5 の cosine 分布 (0.8〜0.9 に密集する) への対処として実測で決めた値 (0.86 は air_city_a の実データ。sea/auto_recall.py:68-77 に測定記録)。一方 **0.78 (`_REFLEX_FLOOR`) と Noul 確率 0.5 は、反射判断による選別の実験 (Jev、2026-09、まはーの実機評価に合格) で調整した値**で、役割は「反射判断に回す候補の下限」(sea/auto_recall.py:146-149, 188-193、docs/intent/auto_recall_jev_rerank.md)。
- e5 用のプレフィックス (`query: ` / `passage: `) を付けるかは「モデル名に e5 を含むか」で分岐している。
- モデル配布は setup の固定ダウンロードと起動時の自動ダウンロードのみ。update 系のスクリプトは配らないので、既存利用者には**起動時の自動ダウンロードが唯一の経路**。
- メッセージの保存では、保存と同じ錠前 (同時書き込みを防ぐ仕組み) を握ったまま、本文のチャンク (120〜480 字) の埋め込みを同期計算している。**保存の待ちに乗るのはクエリではなくチャンクの埋め込み時間で、ベンチの一括処理から換算すると q4 は約 120ms/チャンク (e5 は約 30ms、約 4 倍)。** 実際の保存経路での実測は実装時に行う。

## 設計

### 1. 変換の実装 — Embedder の中身を置き換える

- `sai_memory/memory/recall.py` の `Embedder` を fastembed から **onnxruntime の直接呼び出し**に置き換える。クラス名と `embed(texts, is_query=)` は変えない (テスト約 45 箇所の差し替えが依存)。次元を知らせる面は、今は入れ子 (`embedder.model.embedding_size`) なので、平らな `embedder.embedding_size` に変えて呼び出し側 2 本 (再計算の API と CLI) も同時に直す。**`embedding_size` が返すのは「保存する次元」** — 再計算はこの値と保存済みベクトルの長さを比べるため。
- ONNX グラフは文埋め込み (`sentence_embedding`、768 次元、プーリング・射影・正規化込み) を出力するので、後処理の自前実装は不要。トークナイズは tokenizers で `tokenizer.json` を直接読む。画像・音声用の必須入力 3 つには空テンソルを渡す (ベンチで確認済みの作法)。入力の長さの上限は現行同等 (512 トークンで切り詰め) を既定とし、Chronicle 本文の実際の長さ分布を実装時に測って見直す (ベクトルを作る入力の切り詰めであり、記憶の原文は切らない)。
- プロンプト: クエリ側 `task: search result | query: `、文書側 `title: none | text: `。e5 のモデル名分岐は削除する。
- **モデルの指定口は撤去する** (切り替え不要の裁定に従う): env `SAIMEMORY_EMBED_MODEL` / `_PATH` / `_DIM`、CLI の `--model` / `--model-path` / `--model-dim`、e5-base への自動フォールバック。モデルの同一性は「名前 + 量子化 + revision」を一体で `embed_metadata.embed_model` に記録する (例: `embeddinggemma-2-q4@daa72c51`) — 名前だけの比較では量子化の変更を検出できないため。ローカルのファイル探索 (今は `onnx/model.onnx` 等の決め打ち) も新しいファイル名に合わせて更新する。
- GPU: 現行と同じく providers に CUDA があれば使う。**4bit 量子化の演算 (MatMulNBits) が CUDA の provider で動くかは実装時に実測する (未確認)。** 動かなければ CPU 固定とし、`SAIMEMORY_EMBED_CUDA` と docs/getting-started/gpu-setup.md を実測の結果に合わせて書き換える。
- モデルファイルは `sbert/embeddinggemma-2-q4/` に置く。中身は `model_q4.onnx` + `model_q4.onnx_data` + `tokenizer.json` + `tokenizer_config.json` + `config.json` (revision `daa72c51…` に固定)。setup (bat/sh) と起動時の自動ダウンロードを差し替える。
- **依存の下限を実測して requirements を更新する**: ベンチは tokenizers 0.23.3 / onnxruntime 1.23.2 で通したが、lock の tokenizers は 0.21.4 で、この tokenizer.json が読めるか未確認。MatMulNBits の必要下限も onnxruntime `>=1.17` のままでは保証できない。huggingface-hub (自動ダウンロードに使用) は fastembed を外した後も直接の依存として明示する。

### 2. 移行 — 全ベクトルの再計算

- 起動時のモデル変更の検出を全 4 系統に広げる (今はメッセージだけ)。
- 警告バナーの「実行」ボタンから呼ばれる再計算の対象を広げる: メッセージの再計算 (既存) に加え、**Chronicle / ページ / Fragment の古い次元の行を削除し、その場で再計算まで行う** (次の Metabolism 任せにしない — 不変条件 2 の「完了とともに全面回復」の完了をこのボタンの終了と一致させる)。
- CLI (`scripts/reembed_memory.py`) も同じ範囲にし、記録 (`embed_metadata.embed_model`) の更新漏れを直す。
- 再計算が終わったら警告の表示が消えるように直す (今は再起動まで残る)。
- 所要の見込み: 8.4 texts/s なので 1 万チャンク規模で約 20 分。進捗表示は既存のポーリング UI を使う。再計算が終わるまでは新しく書かれた記憶だけが想起の対象になり、終われば全部が対象に戻る (不変条件 2)。

### 3. しきい値の再測定

- **0.86 / +0.02 (分布由来の組)**: e5 の「cosine が 0.8〜0.9 に密集する」性質への対処だった。EmbeddingGemma 2 は分布が広い (計測: 正解文書との cosine 中央値 0.80、正解以外の最高値の中央値 0.75、正解とそれ以外の差は e5 の約 1.7 倍)。**そのまま持ち越せず、再測定が必須工程。** 測定は 0.86 を決めたときと同じ手順 (話題に沿ったクエリ / 無関係なクエリ / 「うん」のような短文、それぞれでの上位ヒットの分布) を踏襲する。データは隔離環境の合成データを既定とし、元の測定と同等の実データ (本番の memory.db の読み取り専用利用) で確かめる場合はまはーの事前承認を得る。
- **0.78 / 0.5 (反射判断の組)**: こちらは分布測定では決められない — Jev の実験 (2026-09、まはーの実機評価に合格) で「反射判断に回す候補の下限」として調整した値だから。新しい分布に合わせて下限を据え直したうえで、**docs/intent/auto_recall_jev_rerank.md の合否手順を縮小再実行して、反射判断込みの採否が合格時と同等であることを確かめる。**
- しきい値の env (`SAIVERSE_AUTO_RECALL_THRESHOLD` 等) を上書きしている利用者の値は e5 前提のまま残る。この env は docs 未掲載で利用者はほぼいない見込みだが、リリースノートに「上書きしている場合は外して既定に戻す」を明記する。
- 既定値を前提にしたテスト (tests/test_auto_recall_jev.py ほか約 9 箇所) を更新する。

### 4. ドキュメント・設定の更新

同じ変更で直す一覧 (調査 §8 + レビューの補完): docs/reference/environment-vars.md、docs/getting-started/configuration.md、docs/getting-started/gpu-setup.md、.env.example、docs/concepts/legacy/saimemory.md、docs/developer-guide/project-structure.md、docs/third_party_licenses.md (fastembed を外し EmbeddingGemma 2 のライセンスを足す)、sai_memory/README.md、requirements.txt / requirements-gpu.txt / lock、CLAUDE.md (SAIMEMORY_EMBED_MODEL への言及 2 箇所)、docs/intent/dependency_management.md、docs/issues/python_314_support_verification.md、docs/intent/memory_architecture_v2.md と ultramemory.md と auto_recall_jev_rerank.md の e5 前提の節、saiverse/recall_walk.py 冒頭の fastembed への言及、docs/issues/autonomous_v2_post_live_gaps.md の「e5-small がテキスト専用」の記述、docs/intent/saiverse_lite.md (「本体と同モデル」の記述に相違の注記を入れる — Lite 本体の対応は「範囲外」)。

## 分岐 (まはー判断待ち)

**保存する次元 — 768 のまま入れるか、512 に切り詰めて入れるか。** EmbeddingGemma 2 は「768 個の数字の先頭 512 個だけ残しても意味が保たれる」性質 (Matryoshka) を持つ。

| | 768 で保存 | 512 で保存 |
|---|---|---|
| JSQuAD R@5 / R@1 (q4) | 0.9579 / 0.8330 | 0.9550 / 0.8242 |
| JSTS (q4、STS プロンプト) | 0.8553 | 0.8561 |
| memory.db のベクトル部の大きさ | 今の約 2 倍 | 今の約 1.33 倍 |
| 検索 1 回あたりの照合計算 | 今の約 2 倍 | 今の約 1.33 倍 |

検索の照合は毎回全ベクトルを読み直して計算する作りなので、この差は記憶が大きいペルソナほど毎回の想起に効く。精度は 512 だと上位 1 件 (e5 比 -0.019) に加えて上位 5 件も僅かに e5 を下回る (0.9550 対 0.9563)。将来の画像・音声のベクトルにも同じ切り詰めが効くかは**未検証** (公式ページは 128 次元まで切り詰めると多モーダルの品質が目立って落ちると記しており、512 での劣化幅の計測は無い)。

## 範囲外 (次の段)

- **画像・音声の直接埋め込み** (メディアを LLM 要約で文字にする現行機構の置き換え)。ONNX の配布はテキスト・画像・音声が別ファイルで、**テキスト部だけで動くことはベンチで確認済み**。画像・音声の部を追加したときの組み合わせ動作は未計測で、クエリが「1 本の文字列」から「ベクトルの組」になる設計変更も伴うため、本移行が済んでから別 intent で設計する。
- **SAIVerse Lite への反映。** Lite の intent は「本体と同モデル (transformers.js + e5)」と記しており、互換契約 (saiverse_lite.md §5「どちらかが形式を変えるときは同じ変更の中で両側を更新する」) がある。ただし Native エクスポートにはベクトルが含まれず、エクスポート形式が埋め込みモデルを前提にしているかは**未確認** — **実装に入る前に Lite のエクスポート形式を確認し、ベクトルやモデル前提が入っているなら本移行の範囲に引き込む。** 入っていなければ Lite の対応は次の段。
- 検索の照合が毎回全件を JSON から読み直す作りの高速化 (次元増で倍になる費用の緩和策)。本移行では手を付けない。

## 検証手順

1. 隔離環境 (隔離 SAIVERSE_HOME + 合成ペルソナ) に旧 384 次元のベクトルを持つ memory.db を用意し、新版で起動 → 警告バナー → 「実行」→ 4 系統すべてのベクトルが新しい次元で揃い、警告が消え、自動想起に記憶が浮かぶ — までを通しで見る。
2. しきい値の再測定 (§3) を経て、自動想起の採用・棄却が妥当な帯に入ることを隔離環境の会話で確かめる。反射判断込みの採否も同所で見る。
3. まはー自身の操作による本番環境での移行一回 (警告バナーからの再計算)。所要時間と移行後の想起の質を見る。

## 経緯

- 2026-10-10: アイディア帳に起案 (まはー)。論点 4 つのうち「品質と CPU 速度の検証が先」が入口だった。
- 2026-10-12: ワークツリー experiment/embeddinggemma2-bench でベンチ実施 (量子化していない元のモデル → ONNX 量子化 3 種)。4bit 版が「精度ほぼ無傷・ダウンロードと常駐メモリは e5 の半分以下・クエリ 30ms」と判明。まはーの GO (チャット原文「OK！4bit圧縮を前提に移行設計に進もう！切り替え不要！！」)。同日、埋め込みを使っている全箇所の調査を実施して本 intent を起草し、言葉づかいと整合性のレビューを通した (0.78 の出自の誤り、メモリ・JSTS の数字の読み方、モデル指定口の扱いなどを修正)。
