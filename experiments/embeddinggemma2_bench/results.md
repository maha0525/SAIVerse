# embeddinggemma-2 vs multilingual-e5-small ベンチマーク結果

計測日: 2026-10-12。数値の解釈・推奨は含めない (計測事実のみ)。

## 計測条件

### マシン・ソフトウェア

| 項目 | 値 |
|---|---|
| CPU | AMD Ryzen AI 9 HX 370 (12 コア / 24 スレッド) |
| メモリ | 98,374,103,040 bytes (約 91.6 GiB) |
| OS | Windows 11 Pro 10.0.26200 |
| 動作 | 両モデルとも CPU のみ (GPU 不使用) |
| Python | `.venv-exp` (作業ツリー内) |
| e5 側ランタイム | fastembed 0.7.4 / onnxruntime 1.23.2 (CPUExecutionProvider のみ利用可能な環境) |
| gemma 側ランタイム | sentence-transformers 6.1.0 / transformers 5.19.0 / torch 2.14.1+cpu (`torch.get_num_threads()` = 12) |

注: torch のバージョンは実行時に `torch.__version__` が返した値 (2.14.1+cpu) を記載している。

### モデルの読み込み方

- **e5** (`intfloat/multilingual-e5-small`, 384 次元): 本番の `sai_memory.memory.recall.Embedder` クラスをそのまま使用 (`cuda=False`、`SAIMEMORY_EMBED_CUDA=0`)。ローカルスナップショット `C:\Users\shuhe\workspace\SAIVerse\sbert\multilingual-e5-small` (読み取り専用) を `local_model_path` に渡した。fastembed 0.7.4 の組み込みモデル一覧に e5-small は無いため、本番と同じく `add_custom_model` 経由で `onnx/model.onnx` (470 MB、fp32) が読まれる。プレフィックスは Embedder が付ける `"query: "` / `"passage: "`。`Embedder.embed()` は fastembed の既定 batch_size (256) で動き、結果を Python の list に変換して返す (この変換も本番経路の一部として計測に含む)。出力は念のため L2 正規化し直してから cosine を取った。
- **gemma** (`google/embeddinggemma-2`, 768 次元): `SentenceTransformer('google/embeddinggemma-2', device='cpu')`。重みの dtype はチェックポイント既定の **bfloat16** で読まれた。`encode(..., prompt_name=..., normalize_embeddings=True, batch_size=32)`。プロンプト文字列:
  - SearchQuery: `task: search result | query: `
  - Document: `title: none | text: `
  - STS: `task: sentence similarity | query: `
- gemma のチェックポイントはテキスト以外の塔も含み、SentenceTransformer はそれらも全部メモリに載せる。ロード時のパラメータ数:

| サブモジュール | パラメータ数 |
|---|---|
| language_model (テキスト) | 271,002,624 |
| audio_tower | 304,824,608 |
| vision_tower | 167,364,608 |
| embed_audio | 786,432 |
| embed_vision | 393,216 |
| 合計 | 744,371,488 |

- HF キャッシュからの読み込みは `HF_HUB_OFFLINE=1` で行った (ネットワーク確認の待ちをロード時間に含めないため)。

### 補助計測 (速度・メモリのみ)

依頼範囲の 2 モデルに加えて、gemma の読み込み方を変えた 2 条件を速度・メモリだけ測った。検索精度は測っていない (どちらも出力は既定 gemma と同じか、数値誤差の範囲)。

- **gemma_fp32**: `model_kwargs={"dtype": torch.float32}` で重みを float32 で読む。1 本のクエリで bf16 版との cosine は 0.9983 だった。
- **gemma_textonly**: 既定 (bf16) で読んだ直後に `vision_tower` / `audio_tower` / `embed_vision` / `embed_audio` を `None` に差し替えて GC する実験用の手当て。sentence-transformers が用意している読み込み方ではない。差し替え前後で同じクエリの埋め込みが完全一致 (最大差 0.0) することを確認した。

## A. 速度・メモリ (bench_speed.py)

### 方法

- 親プロセスが、モデル (条件) ごとに `bench_speed.py --role <名前>` を **別の子プロセス** (`python -X utf8 -I`) として順番に起動する。同時に走る子は常に 1 つ。各子プロセスは 1 つのモデルだけを読み込むので、RSS はそのモデルだけのもの。
- これを 3 巡 (e5 → gemma → gemma_fp32 → gemma_textonly を 3 回) 繰り返した。表の値は **3 巡の中央値**、括弧内は 3 巡の最小〜最大。
- 単発クエリ: 21〜28 字の日本語クエリ 30 本 (スクリプト内に自作、思い出し検索風) を 1 本ずつ encode。e5 は `Embedder.embed([q], is_query=True)`、gemma は `prompt_name='SearchQuery'`。最初の 3 本をウォームアップとして除いた 27 本の median / p95。
- バッチ: 159〜400 字 (平均 287 字) の日記・会話風チャンク 200 本 (60 文を乱数固定 seed=20261012 で組み合わせて生成) を 1 回の呼び出しで encode。e5 は `is_query=False` (passage:)、gemma は `prompt_name='Document'`。
- メモリ: psutil の `memory_info()` (Windows では `rss` = WorkingSetSize、`peak_wset` = PeakWorkingSetSize、`private` = PagefileUsage)。
- 「import 時間」は fastembed / sentence-transformers+torch などのライブラリ import の時間、「モデルロード時間」はモデルオブジェクト構築の時間 (別々に測った)。

### 結果

| 項目 | e5 | gemma (bf16, 既定) | gemma_fp32 | gemma_textonly |
|---|---|---|---|---|
| import 時間 (s) | 1.77 (1.40〜1.80) | 13.69 (12.64〜14.01) | 13.20 (12.79〜13.86) | 13.46 (12.79〜14.52) |
| モデルロード時間 (s) | 1.13 (1.09〜1.26) | 4.95 (4.80〜5.07) | 5.86 (5.28〜6.16) | 5.16 (4.66〜5.27) |
| 初回クエリ (ms, ウォームアップ前の 1 本目) | 10.0 (9.9〜10.9) | 132.0 (127.0〜134.8) | 158.6 (152.8〜164.1) | 126.7 (114.3〜159.7) |
| **単発クエリ median (ms)** | **6.2** (6.2〜6.3) | **91.8** (89.9〜99.8) | 126.9 (125.3〜142.3) | 88.2 (85.5〜91.4) |
| 単発クエリ p95 (ms) | 7.7 (7.4〜7.7) | 102.3 (99.9〜106.8) | 141.7 (140.6〜172.1) | 100.4 (95.4〜103.4) |
| 単発クエリ 3 巡 81 本をまとめた median / p95 (ms) | 6.2 / 7.6 | 93.0 / 105.9 | 131.6 / 160.3 | 87.9 / 101.9 |
| バッチ 200 件の所要時間 (s) | 5.91 (5.85〜5.96) | 22.75 (22.30〜22.87) | 38.92 (38.41〜39.15) | 21.98 (21.79〜22.05) |
| **バッチスループット (texts/s)** | **33.9** (33.6〜34.2) | **8.8** (8.8〜9.0) | 5.1 (5.1〜5.2) | 9.1 (9.1〜9.2) |
| RSS 起動直後 (MB) | 22 | 22 | 22 | 22 |
| RSS import 後 (MB) | 94 | 424 | 424 | 424 |
| **RSS ロード後 (MB)** | **805** | **2,115** | 3,532 | 1,201 |
| RSS 単発クエリ 30 本後 (MB) | 809 | 2,139 | 3,581 | 1,235 |
| **RSS バッチ後 (MB)** | **2,210** | **2,253** | 3,673 | 1,351 |
| ピーク WorkingSet (MB, プロセス全期間) | 2,480 | 3,232 | 4,910 | 2,336 |
| Private bytes バッチ後 (MB) | 3,662 | 3,745 | 6,229 | 2,871 |
| 埋め込み次元 | 384 | 768 | 768 | 768 |

観測事実の補足:

- e5 の RSS はロード後 805 MB からバッチ後 2,210 MB に増えた。fastembed の既定 batch_size 256 により 200 本が 1 回のバッチで ONNX Runtime に渡されている (本番の `Embedder.embed()` も同じ既定値で呼んでいる)。
- gemma の RSS 増加はロード後 2,115 MB → バッチ後 2,253 MB (batch_size 32)。

生データ: `results/speed.json` (3 巡分)、`results/speed_<role>_run<N>.json` (各子プロセスの出力、30 本全部の個別レイテンシを含む)。

## B. 検索精度 (bench_retrieval.py)

### データ

JGLUE (github.com/yahoojapan/JGLUE, main ブランチ) から raw JSON をダウンロードした。依頼文の想定パス (jsts-v1.1) はリポジトリに無く、現行は両方とも v1.3 だった。

| ファイル | 取得元 | サイズ |
|---|---|---|
| `data/jsquad-valid-v1.3.json` | `datasets/jsquad-v1.3/valid-v1.3.json` | 1,976,638 bytes |
| `data/jsts-valid-v1.3.json` | `datasets/jsts-v1.3/valid-v1.3.json` | 372,253 bytes |

### JSQuAD 検索評価の条件

- 文書集合: valid の全段落 context をユニーク化した **1,145 件** (絞り込みなし)。context は `タイトル [SEP] 本文` 形式なので、最初の ` [SEP] ` を改行に置き換えて `タイトル\n本文` として埋め込んだ。文書の長さは中央値 163 字、最大 900 字。
- クエリ: valid の全質問 **4,442 問** (サブサンプルなし。`is_impossible` の質問は 0 件)。質問の長さは中央値 25 字。
- 各質問を cosine 類似度で全 1,145 文書と比べ、正解 context の順位で Recall@1 / @5 / @10 と MRR@10 を出した (同点は正解に有利に数える: 正解より厳密に高い文書の数 + 1 を順位とした)。
- e5: クエリ `query: `、文書 `passage: `。gemma: クエリ `SearchQuery`、文書 `Document`。
- gemma の 512 / 256 次元は、768 次元の埋め込みの先頭を切り詰めて L2 正規化し直したもの (再 encode なし)。
- トークン長: e5 のトークナイザ (tokenizer.json、`passage: ` 付き) で 512 トークンを超える文書は 1 件 (最大 530、中央値 106)。fastembed の最大長 512 で切り詰められる対象になる。gemma (Document プロンプト付き) は最大 512 トークン・中央値 108 で、512 超えは 0 件 (gemma 側は切り詰めなしで計測)。

### JSQuAD 検索結果

| モデル | 次元 | Recall@1 | **Recall@5** | Recall@10 | MRR@10 |
|---|---|---|---|---|---|
| e5 | 384 | 0.8431 | **0.9563** | 0.9739 | 0.8917 |
| gemma | 768 | 0.8370 | **0.9586** | 0.9759 | 0.8902 |
| gemma | 512 | 0.8305 | 0.9563 | 0.9757 | 0.8857 |
| gemma | 256 | 0.8091 | 0.9491 | 0.9694 | 0.8693 |

### JSQuAD 類似度の分布 (しきい値の参考)

「正解文書との類似度」= 各質問と正解 context の cosine。「正解以外の最高類似度」= 正解を除いた 1,144 文書の中での最大 cosine。「差」= 前者 − 後者 (質問ごとに計算してから集計)。全 4,442 問。

| モデル | 次元 | 系列 | mean | p10 | p50 | p90 |
|---|---|---|---|---|---|---|
| e5 | 384 | 正解文書との類似度 | 0.8902 | 0.8438 | 0.8945 | 0.9299 |
| e5 | 384 | 正解以外の最高類似度 | 0.8622 | 0.8219 | 0.8641 | 0.8991 |
| e5 | 384 | 差 | 0.0279 | -0.0085 | 0.0278 | 0.0642 |
| gemma | 768 | 正解文書との類似度 | 0.7951 | 0.7209 | 0.8032 | 0.8551 |
| gemma | 768 | 正解以外の最高類似度 | 0.7443 | 0.6770 | 0.7466 | 0.8069 |
| gemma | 768 | 差 | 0.0509 | -0.0152 | 0.0496 | 0.1200 |
| gemma | 512 | 正解文書との類似度 | 0.7985 | 0.7241 | 0.8073 | 0.8582 |
| gemma | 512 | 正解以外の最高類似度 | 0.7489 | 0.6807 | 0.7513 | 0.8114 |
| gemma | 512 | 差 | 0.0496 | -0.0165 | 0.0478 | 0.1179 |
| gemma | 256 | 正解文書との類似度 | 0.8099 | 0.7359 | 0.8188 | 0.8683 |
| gemma | 256 | 正解以外の最高類似度 | 0.7655 | 0.6994 | 0.7691 | 0.8270 |
| gemma | 256 | 差 | 0.0444 | -0.0184 | 0.0437 | 0.1108 |

### JSTS (STS) の条件と結果

- valid 全 1,457 ペア (ユニーク文 2,808 件をそれぞれ 1 回ずつ encode)。ペアの cosine と人手スコア (label, 0〜5) の Spearman 相関 (scipy.stats.spearmanr)。参考に Pearson も記載。
- 主条件 (primary) は、両側に同じプレフィックス/プロンプトを付ける対称設定: e5 は両側 `query: `、gemma は両側 `STS`。
- 補助条件: e5 の両側 `passage: `、gemma の両側 `Document` と両側 `SearchQuery`。gemma は各条件について 768 / 512 / 256 次元 (切り詰め + 再正規化) も出した。

| モデル | 条件 | 次元 | **Spearman** | Pearson |
|---|---|---|---|---|
| e5 | query: / query: (primary) | 384 | **0.7892** | 0.8344 |
| e5 | passage: / passage: | 384 | 0.7791 | 0.8230 |
| gemma | STS / STS (primary) | 768 | **0.8580** | 0.8949 |
| gemma | STS / STS (primary) | 512 | 0.8590 | 0.8953 |
| gemma | STS / STS (primary) | 256 | 0.8588 | 0.8918 |
| gemma | Document / Document | 768 | 0.8284 | 0.8698 |
| gemma | Document / Document | 512 | 0.8292 | 0.8710 |
| gemma | Document / Document | 256 | 0.8287 | 0.8701 |
| gemma | SearchQuery / SearchQuery | 768 | 0.8438 | 0.8877 |
| gemma | SearchQuery / SearchQuery | 512 | 0.8449 | 0.8884 |
| gemma | SearchQuery / SearchQuery | 256 | 0.8442 | 0.8855 |

### 精度計測の実行時間 (参考、1 回のみ・同一プロセスで e5 → gemma の順)

| 項目 | e5 | gemma |
|---|---|---|
| モデルロード (s) | 2.5 | 17.3 |
| JSQuAD 文書 1,145 件の encode (s) | 61.9 | 113.3 |
| JSQuAD 質問 4,442 問の encode (s) | 35.2 | 89.0 |

生データ: `results/retrieval.json`。

## 計測上の妥協・注意点

- サブサンプル・文書集合の絞り込みはしていない (JSQuAD 全 4,442 問 × 全 1,145 文書、JSTS 全 1,457 ペア)。
- 速度は 1 台 (上記 CPU) だけで測った。gemma_fp32 と既定 (bf16) の速度差は、この CPU が bf16 演算をどう扱うかに依存する可能性があり、他の CPU で同じ比になるかは未確認。
- 速度計測は 3 巡、精度計測の実行時間は 1 回だけ。
- 精度計測の e5 と gemma は同じプロセスで順に実行した (精度には影響しないが、その表の実行時間は参考値)。
- gemma_textonly は実験用の手当てで、本番で使える読み込み方として確立したものではない。
- JSQuAD の文書に付けた gemma の Document プロンプトは `title: none` のまま (タイトルは本文の先頭行として入れた)。

## C. ONNX 量子化版 (onnx-community/embeddinggemma-2-ONNX)

計測日: 2026-10-12。A・B 節と同じマシン・同じ `.venv-exp`。数値の解釈・推奨は含めない。

### 対象とダウンロードサイズ

HF リポジトリ `onnx-community/embeddinggemma-2-ONNX` (revision `daa72c51243991dfcaf9f9137d2c573d8f7790c0` に固定) から、テキストモデルの 3 変種とトークナイザ類だけを `snapshot_download(allow_patterns=...)` で取得した。画像・音声の encoder (`vision_encoder*` / `audio_encoder*`) は取得していない。

| 役割名 (このベンチ内) | ファイル | `.onnx` (bytes) | `.onnx_data` (bytes) | 重みの形式 (グラフを読んで確認) |
|---|---|---|---|---|
| gemma_onnx_fp16 | `onnx/model_fp16.onnx` | 425,780 | 542,085,120 | 初期化子が FLOAT16 (MatMul 194 個) |
| gemma_onnx_q8 | `onnx/model_quantized.onnx` | 495,165 | 313,724,928 | `MatMulNBits` 146 個、全て bits=8 / block_size=32 |
| gemma_onnx_q4 | `onnx/model_q4.onnx` | 490,742 | 174,028,800 | `MatMulNBits` 146 個、全て bits=4 / block_size=32 |

共通: `tokenizer.json` 32,170,510 bytes (google/embeddinggemma-2 のものとバイト一致を `cmp` で確認)、`tokenizer_config.json` 1,599 bytes、`config.json` 5,031 bytes、`config_sentence_transformers.json` 1,565 bytes (プロンプト文字列の確認用)。

### グラフの入出力と後処理の所在

onnxruntime の `get_inputs()` / `get_outputs()` の結果 (3 変種で同一):

| 種別 | 名前 | 型 | 形 |
|---|---|---|---|
| 入力 | `input_ids` | int64 | [batch_size, sequence_length] |
| 入力 | `attention_mask` | int64 | [batch_size, sequence_length] |
| 入力 | `image_features` | float | [num_image_tokens, 512] |
| 入力 | `video_features` | float | [num_video_tokens, 512] |
| 入力 | `audio_features` | float | [num_audio_tokens, 512] |
| 出力 (0 番目) | `last_hidden_state` | float | [batch_size, sequence_length, 768] |
| 出力 (1 番目) | `sentence_embedding` | float | [batch_size, 768] |

- グラフのノードを `onnx` パッケージで読んで辿った結果: `last_hidden_state` は最終 norm の後の `/model/embedding_projection/MatMul` (隠れ次元 512 → 768) の出力。`sentence_embedding` は `last_hidden_state` を `attention_mask` でマスクした平均 (`/model/pooling/ReduceSum` ÷ トークン数、下限 1e-9) を L2 ノルム (下限 1e-12) で割ったもの。つまり射影・平均プーリング・正規化の全てがグラフ内にある。
- sentence-transformers 側 (`google/embeddinggemma-2` の `modules.json`) の構成は `Transformer` → `Pooling` (mean、`include_prompt: true`) → `Normalize` の 3 段で、Dense 層は無い。transformers 5.19.0 の `modeling_embedding_gemma2.py` では `embedding_projection` はテキスト本体の forward の中で `last_hidden_state` に掛けられている。
- このため ONNX 版のラッパー (`common.GemmaOnnxWrapper`) は後処理を自前で再現せず、`sentence_embedding` 出力をそのまま使い、fp32 で L2 正規化し直している。
- テキストだけの呼び出しでも `image_features` / `video_features` / `audio_features` は必須入力で、省くと onnxruntime が `Required inputs ... are missing` で失敗する。ラッパーは形 [0, 512] の空テンソルを渡している。

### ラッパーの条件

- onnxruntime 1.23.2、`CPUExecutionProvider`、`SessionOptions` は既定値のまま (intra_op threads も既定)。tokenizers 0.23.3 で `tokenizer.json` を直接読み、切り詰めなし (sentence-transformers 側の `max_seq_length` も 1e30 で、実質切り詰めなし)。
- バッチの作り方は `SentenceTransformer.encode` に合わせた: 長い順に並べて batch_size 32 で区切り、`<pad>` (id 0) で右詰めパディング、結果を元の順に戻す。
- プロンプト文字列は A・B 節と同じ (SearchQuery `task: search result | query: ` / Document `title: none | text: ` / STS `task: sentence similarity | query: `)。

### 検算 (check_onnx_parity.py)

日本語 10 本 (`bench_speed.py` の QUERIES 先頭 5 本 + SENTENCES 先頭 5 本) を 3 種のプロンプトそれぞれで埋め込み (計 30 本)、sentence-transformers の埋め込みとの cosine を取った。

- 参照の注意: sentence-transformers を bf16 (既定) で読んだ場合、`normalize_embeddings=True` の出力ノルムは 0.99810〜1.00384 で 1 ちょうどではなかった (fp32 で読んだ場合は 1 ± 1.2e-7)。cosine は参照側も fp32 で正規化し直してから計算した。正規化し直さずに内積を取ると、この 10 本では fp16 版との値が最大 1.0038 になった (1 を超える)。
- トークン列: tokenizers で作った id 列は、SentenceTransformer のトークナイザが同じ「プロンプト + 本文」に対して返す id 列と 30 本全てで一致した。
- パディングの影響: 10 本をまとめて 1 バッチにした場合と 1 本ずつの場合の cosine の最小値は 3 変種とも 1.000000。

| 変種 | vs ST bf16: min / mean / max (30 本) | vs ST fp32: min / mean / max (30 本) |
|---|---|---|
| gemma_onnx_fp16 | 0.99986 / 0.99996 / 0.99998 | 1.00000 / 1.00000 / 1.00000 |
| gemma_onnx_q8 | 0.99973 / 0.99992 / 0.99995 | 0.99974 / 0.99995 / 0.99997 |
| gemma_onnx_q4 | 0.97279 / 0.98841 / 0.99314 | 0.97302 / 0.98842 / 0.99312 |
| (参考) ST bf16 vs ST fp32 | 0.99987 / 0.99997 / 0.99998 | — |

SearchQuery プロンプトの 10 本ごとの値 (vs ST bf16):

| # | テキスト (先頭 20 字) | fp16 | q8 | q4 |
|---|---|---|---|---|
| 0 | 先週の日曜日にまはーと一緒に行った公園の | 0.9999 | 0.9998 | 0.9826 |
| 1 | 前に話していた新しいゲームの発売日はいつ | 1.0000 | 1.0000 | 0.9898 |
| 2 | 雨の日に聴きたいって言っていた曲のタイト | 1.0000 | 0.9999 | 0.9911 |
| 3 | 昨日の夜に作ったカレーの味付けについて話 | 1.0000 | 0.9999 | 0.9899 |
| 4 | 引っ越しの準備でまだ終わっていない作業は | 1.0000 | 0.9997 | 0.9728 |
| 5 | 今日は朝から雨が降っていて、窓の外がずっ | 1.0000 | 0.9999 | 0.9920 |
| 6 | まはーはいつもより少し遅く起きてきて、眠 | 1.0000 | 0.9999 | 0.9865 |
| 7 | 朝ごはんはトーストと目玉焼きにして、コー | 1.0000 | 0.9999 | 0.9870 |
| 8 | 午前中は部屋の掃除をして、ずっと放置して | 1.0000 | 0.9999 | 0.9902 |
| 9 | 昼過ぎに近所のスーパーまで買い物に出かけ | 1.0000 | 0.9999 | 0.9890 |

生データ: `results/onnx_parity.json` (3 プロンプト × 10 本の個別値を含む)。

### 速度・メモリ (bench_speed.py、A 節と同じ方法)

`bench_speed.py --roles gemma_onnx_fp16,gemma_onnx_q8,gemma_onnx_q4 --repeat 3 --summary speed_onnx.json` で、役割ごとに別の子プロセスを順番に起動して 3 巡測った。クエリ 30 本・バッチ 200 本のテキストは A 節と同一。表の値は 3 巡の中央値、括弧内は最小〜最大。e5 と gemma (bf16) の列は A 節の値の再掲 (今回は再計測していない)。「import 時間」は ONNX 版では onnxruntime / tokenizers / huggingface_hub の import。

| 項目 | e5 (A 節) | gemma bf16 (A 節) | gemma_onnx_fp16 | gemma_onnx_q8 | gemma_onnx_q4 |
|---|---|---|---|---|---|
| import 時間 (s) | 1.77 | 13.69 | 0.30 (0.28〜0.67) | 0.70 (0.69〜0.79) | 0.32 (0.26〜0.68) |
| モデルロード時間 (s) | 1.13 | 4.95 | 3.01 (2.92〜3.03) | 1.83 (1.59〜1.84) | 1.90 (1.67〜1.94) |
| 初回クエリ (ms) | 10.0 | 132.0 | 95.8 (90.3〜95.9) | 362.3 (339.4〜404.5) | 35.6 (34.8〜39.5) |
| **単発クエリ median (ms)** | **6.2** | **91.8** | **88.0** (84.5〜90.9) | **328.5** (326.7〜465.0) | **29.8** (28.7〜30.8) |
| 単発クエリ p95 (ms) | 7.7 | 102.3 | 95.7 (95.6〜102.1) | 356.7 (339.7〜758.7) | 33.3 (30.3〜33.9) |
| 単発クエリ 3 巡 81 本をまとめた median / p95 (ms) | 6.2 / 7.6 | 93.0 / 105.9 | 88.3 / 100.8 | 331.4 / 570.1 | 29.8 / 33.7 |
| バッチ 200 件の所要時間 (s) | 5.91 | 22.75 | 116.97 (113.97〜124.75) | 28.82 (28.26〜28.97) | 23.92 (22.57〜25.73) |
| **バッチスループット (texts/s)** | **33.9** | **8.8** | **1.7** (1.6〜1.8) | **6.9** (6.9〜7.1) | **8.4** (7.8〜8.9) |
| RSS 起動直後 (MB) | 22 | 22 | 22 | 22 | 22 |
| RSS import 後 (MB) | 94 | 424 | 55 | 55 | 55 |
| **RSS ロード後 (MB)** | **805** | **2,115** | **1,247** (1,246〜1,247) | **158** (157〜158) | **223** (223〜223) |
| RSS 単発クエリ 30 本後 (MB) | 809 | 2,139 | 1,251 (1,251〜1,252) | 324 (323〜324) | 259 (258〜259) |
| **RSS バッチ後 (MB)** | **2,210** | **2,253** | **2,464** (2,413〜2,786) | **2,394** (2,394〜2,394) | **2,330** (2,329〜2,331) |
| ピーク WorkingSet (MB, プロセス全期間) | 2,480 | 3,232 | 2,464 (2,413〜2,786) | 2,417 (2,417〜2,418) | 2,330 (2,329〜2,331) |
| Private bytes バッチ後 (MB) | 3,662 | 3,745 | 3,536 (3,536〜3,536) | 3,003 (3,002〜3,004) | 3,070 (3,069〜3,071) |
| 埋め込み次元 | 384 | 768 | 768 | 768 | 768 |

観測事実の補足:

- q8 の単発クエリ (1 本ずつ) は 3 巡とも 300 ms 台で、fp16 (88 ms) と q4 (30 ms) より遅かった。一方、q8 のバッチ 200 件 (batch_size 32) は 28.8 s で fp16 (117 s) より速かった。同じラッパー・同じ入力で起きている。原因は調べていない。
- fp16 のバッチ 200 件は 117 s (1.7 texts/s) で、3 変種の中で最も遅かった。
- q8 と q4 の「RSS ロード後」は `.onnx_data` のファイルサイズ (314 MB / 174 MB) より小さかった。計測値は WorkingSet (物理メモリに載っているページ) なので、ロード直後にまだ触られていないページは含まれない。
- 3 変種とも、バッチ後の RSS は 2.3〜2.5 GB まで増えた。

生データ: `results/speed_onnx.json` (3 巡分)、`results/speed_gemma_onnx_<変種>_run<N>.json` (各子プロセスの出力)。A 節の `results/speed.json` と `results/speed_<role>_run<N>.json` は上書きしていない。

### 検索精度 (bench_retrieval.py、B 節と同じ条件)

`bench_retrieval.py --models gemma_onnx_q8,gemma_onnx_q4 --out retrieval_onnx.json`。JSQuAD 全 4,442 問 × 全 1,145 文書、JSTS 全 1,457 ペア。プロンプト・512/256 次元の作り方 (先頭を切り詰めて再正規化) も B 節と同じ。fp16 は依頼範囲外として測っていない (検算の cosine のみ)。e5 と gemma (bf16) の行は B 節の値の再掲。

トークン長: q8・q4 とも Document プロンプト付きで最大 512 トークン・中央値 108、512 超えは 0 件 (B 節の gemma と同じ値)。

JSQuAD 検索:

| モデル | 次元 | Recall@1 | **Recall@5** | Recall@10 | MRR@10 |
|---|---|---|---|---|---|
| e5 (B 節) | 384 | 0.8431 | **0.9563** | 0.9739 | 0.8917 |
| gemma bf16 (B 節) | 768 | 0.8370 | **0.9586** | 0.9759 | 0.8902 |
| gemma_onnx_q8 | 768 | 0.8368 | **0.9584** | 0.9761 | 0.8902 |
| gemma_onnx_q8 | 512 | 0.8318 | 0.9563 | 0.9755 | 0.8867 |
| gemma_onnx_q8 | 256 | 0.8089 | 0.9498 | 0.9698 | 0.8693 |
| gemma_onnx_q4 | 768 | 0.8330 | **0.9579** | 0.9757 | 0.8871 |
| gemma_onnx_q4 | 512 | 0.8242 | 0.9550 | 0.9737 | 0.8812 |
| gemma_onnx_q4 | 256 | 0.7960 | 0.9464 | 0.9692 | 0.8616 |

JSQuAD 類似度の分布 (768 次元、定義は B 節と同じ):

| モデル | 系列 | mean | p10 | p50 | p90 |
|---|---|---|---|---|---|
| gemma_onnx_q8 | 正解文書との類似度 | 0.7952 | 0.7218 | 0.8038 | 0.8551 |
| gemma_onnx_q8 | 正解以外の最高類似度 | 0.7443 | 0.6770 | 0.7465 | 0.8073 |
| gemma_onnx_q8 | 差 | 0.0509 | -0.0149 | 0.0496 | 0.1195 |
| gemma_onnx_q4 | 正解文書との類似度 | 0.7943 | 0.7198 | 0.8018 | 0.8547 |
| gemma_onnx_q4 | 正解以外の最高類似度 | 0.7448 | 0.6784 | 0.7465 | 0.8070 |
| gemma_onnx_q4 | 差 | 0.0495 | -0.0162 | 0.0475 | 0.1196 |

512 / 256 次元の分布は `results/retrieval_onnx.json` にある。

JSTS:

| モデル | 条件 | 次元 | **Spearman** | Pearson |
|---|---|---|---|---|
| e5 (B 節) | query: / query: (primary) | 384 | **0.7892** | 0.8344 |
| gemma bf16 (B 節) | STS / STS (primary) | 768 | **0.8580** | 0.8949 |
| gemma_onnx_q8 | STS / STS (primary) | 768 | **0.8581** | 0.8950 |
| gemma_onnx_q8 | STS / STS (primary) | 512 | 0.8591 | 0.8955 |
| gemma_onnx_q8 | STS / STS (primary) | 256 | 0.8589 | 0.8920 |
| gemma_onnx_q8 | Document / Document | 768 | 0.8281 | 0.8696 |
| gemma_onnx_q8 | SearchQuery / SearchQuery | 768 | 0.8436 | 0.8877 |
| gemma_onnx_q4 | STS / STS (primary) | 768 | **0.8553** | 0.8942 |
| gemma_onnx_q4 | STS / STS (primary) | 512 | 0.8561 | 0.8944 |
| gemma_onnx_q4 | STS / STS (primary) | 256 | 0.8559 | 0.8910 |
| gemma_onnx_q4 | Document / Document | 768 | 0.8210 | 0.8647 |
| gemma_onnx_q4 | SearchQuery / SearchQuery | 768 | 0.8372 | 0.8840 |

Document / SearchQuery 条件の 512 / 256 次元は `results/retrieval_onnx.json` にある。

精度計測の実行時間 (参考、1 回のみ・同一プロセスで q8 → q4 の順):

| 項目 | gemma_onnx_q8 | gemma_onnx_q4 |
|---|---|---|
| モデルロード (s) | 4.0 | 1.7 |
| JSQuAD 文書 1,145 件の encode (s) | 130.1 | 96.5 |
| JSQuAD 質問 4,442 問の encode (s) | 160.1 | 85.6 |

生データ: `results/retrieval_onnx.json`。B 節の `results/retrieval.json` は上書きしていない。

### C 節の計測上の妥協・注意点

- 検算は 10 本 × 3 プロンプトの 30 本だけ。
- B 節の gemma 768 次元の行は、`GemmaWrapper` が返す bf16 で正規化されたベクトル (ノルムが 1 から最大 0.4% 程度ずれる) のまま内積を取っている (512 / 256 次元は再正規化済み)。C 節の ONNX 版は fp32 で正規化し直したベクトルで内積を取っている。この違いが B 節と C 節の 768 次元の数値差にどれだけ効いているかは確認していない。
- 補助計測の節にある「gemma_fp32 と bf16 版の cosine 0.9983」は、この節の検算 (参照を正規化し直した後で min 0.99987) と値が違う。0.9983 の算出方法 (正規化し直したかどうか) はこの節では確認していない。
- 速度は 3 巡、精度計測の実行時間は 1 回だけ。ONNX 版のスレッド数は onnxruntime の既定値で、torch 側 (12 スレッド) と揃えていない。
- グラフの読み取りのため、`.venv-exp` に `onnx` パッケージ (1.23.2) を追加で入れた。
