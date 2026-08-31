# Intent Document: llama.cpp multimodal slot save/restore 実装 (SAIVerse 専用 fork)

> **Scope**: 本ドキュメントは SAIVerse 本体ではなく、SAIVerse が依存する `ggml-org/llama.cpp` のマルチモーダル KV キャッシュ永続化対応の設計を記す。実装は `temp/llama-fork/` (fork: `maha0525/llama.cpp`) の主線ブランチ **`saiverse/main`** にある。upstream への PR は当面投げない（§2-5 参照）。ブランチ運用は §13 を参照。

## 0. 現在の実装状態 (2026-07-23)

- TurboQuant fork と画像/音声 slot cache は、同じ `maha0525/llama.cpp` リポジトリ内の専用ブランチとして共存できる。追加 fork は不要。
- TurboQuant の checkpoint sidecar (`.ckpt`) を維持したまま、KV state (`.bin`) と multimodal sidecar (`.bin.mtmd`) を保存・復元する。
- outer sidecar と chunk serialization の現行版は v3。原実装の v2 sidecar/chunk は読み込み互換を維持する。
- TurboQuant 側で変わった `clip_image_f32_batch` の `unique_ptr` 所有構造、`grid_x/grid_y`、`add_viewsep/add_newline`、画像/音声フラグを v3 形式へ反映した。
- mmproj SHA-256、サイズ上限、終端・余剰データ、media range の重複/欠落を検証し、復元失敗時は KV と media metadata を残さない。
- CPU/CUDA Release で `llama-server` をビルド済み。`test-mtmd-c-api`、`test-mtmd-sidecar`、TurboQuant を含む `test-quantize-fns` は成功済み。
- `gemma-4-E4B-it-UD-Q4_K_XL.gguf` + `mmproj-F32.gguf` で画像入り slot の save/erase/restore と、server プロセス再起動後の restore を実機確認済み。
- TurboQuant の checkpoint 間隔は logical token 数で判定されるため、1 logical token が数百 position を占める media 後 checkpoint が作られない問題が実機試験で判明した。multimodal prompt では間隔制限を迂回して post-media checkpoint を作成するよう修正し、restore 後に画像 encode が再実行されないことをログで確認した。
- SAIVerse 統合試験は未実施。
- 変更はステージまでとし、commit/push は明示承認を待つ。

## 1. 目的

SAIVerse のペルソナごとの KV キャッシュ永続化 (`llm_clients/llama_cache.py`) を、画像/音声を含むマルチモーダル会話でも動作させる。

現状、`llama-server` に `--mmproj` を渡してマルチモーダル対応で起動すると、`/slots/{id}?action=save` および `?action=restore` API が `check_no_mtmd()` ガードで弾かれ、HTTP 501 `"This feature is not supported by multimodal"` が返る (`server-context.cpp:1566-1572`)。

SAIVerse のペルソナはマルチモーダル前提のため、テキスト専用ペルソナだけを救う対応では意味がない。upstream の対応待ちが望めない (issue #19466 が stale auto-close、reopen 要求もスルー) ため、自力で実装して **SAIVerse 専用 fork として公開**する。SAIVerse 側では `README` で「マルチモーダルキャッシュ使う場合は fork 版を使ってね」と案内する形を取る。

設計方針は ggerganov の 2026-03-19 コメント方針に沿う:
> server_tokens (mtmd chunks と一緒に) を別途保存するロジックを llama-server に追加する必要がある。

## 2. 守るべき不変条件

1. **既存テキスト専用キャッシュとの後方互換**: `--mmproj` なしで保存された既存キャッシュは、修正後の server でも変わらず読み書きできる。ファイルフォーマットを破壊しない。
2. **画像/音声の埋め込みは KV キャッシュ側で復元**: 画像エンコーダ (`mctx`) を再実行する必要はない。KV キャッシュには画像トークン位置に埋め込みベクトルが既に展開済みなので、sidecar はあくまで「どの位置に何があったか」のメタデータのみ保存する。
3. **モデル/mmproj 不一致は明示的に拒否**: 異なる mmproj で保存したキャッシュを別 mmproj で復元しようとした場合は明確にエラーを返す。破綻させない。
4. **mmproj 無しサーバーで sidecar 付きキャッシュを誤読しない**: マルチモーダルで保存したキャッシュを mmproj なしで起動した llama-server が読もうとした場合は、エラー応答で拒否する (`server_tokens.tokens` に `LLAMA_TOKEN_NULL` プレースホルダが含まれるため、KV のみ復元しても後続推論で破綻する)。
5. **fork は upstream に追従可能な状態を保つ**: 余計なリファクタや非関連変更を含めない。upstream master の更新を定期的にマージできる差分量にとどめる。将来 upstream にコントリビュートする可能性も残す。

## 3. 該当コード (upstream master @ 5d5d2e15d 時点)

| 場所 | 行 | 内容 |
|---|---|---|
| `tools/server/server-context.cpp` | 1566-1572 | `check_no_mtmd()` ガード |
| `tools/server/server-context.cpp` | 1980-1996 | `SLOT_SAVE` ハンドラ |
| `tools/server/server-context.cpp` | 2021-2067 | `SLOT_RESTORE` ハンドラ |
| `tools/server/server-context.cpp` | 2068-2096 | `SLOT_ERASE` ハンドラ |
| `tools/server/server-common.h` | 128 | `struct server_tokens` 定義 (`map_idx_to_media`, `tokens`, `has_mtmd`) |
| `tools/mtmd/mtmd.cpp` | 86-91 | `struct mtmd_input_chunk` (opaque、TEXT/IMAGE/AUDIO) |
| `tools/mtmd/mtmd.cpp` | 43-68 | `struct mtmd_image_tokens` (nx, ny, pos_type, image_idx, batch_f32, id) |
| `tools/mtmd/mtmd.cpp` | 71-84 | `struct mtmd_audio_tokens` (n_tokens, batch_f32, id) |
| `src/llama-context.cpp` | 3709-3729 | `llama_state_seq_save_file` / `load_file` 実装 (触らない) |
| `include/llama.h` | 846-857 | 同 API 宣言 (触らない) |

## 4. 設計案: 2ファイル構成 sidecar

```
cache.bin       ← 既存 KV キャッシュ (llama_state_seq_save_file の出力をそのまま)
cache.bin.mtmd  ← 新規 sidecar (server_tokens の map_idx_to_media を serialize)
```

採用理由:
- `llama_state_seq_save_file()` の API シグネチャを壊さない
- mmproj 無しサーバーは `.mtmd` sidecar の存在を知らなくていい (既存通り動く)
- 既存キャッシュとの互換性が「sidecar が無いだけ」で自然に取れる
- ggerganov コメントの「server 側で別途保存」方針と整合

却下した代替案:
- **単一ファイル化** (KV と mtmd メタを連結): 後方互換性を保つにはマジックナンバー判別が必要で複雑化
- **kaetemi 提案の chunk-based API** (`llama_state_seq_save_file_chunk_*`): 大規模リファクタ。fork 維持コストを上げる

## 5. ファイルフォーマット (`.mtmd` sidecar)

```
[magic        u8[4]   ] = "MTMD"
[version      u32     ] = 2 or 3 (v3 が現行、v2 を読み込み可能)
[mmproj_hash  u8[32]  ]    SHA-256 of mmproj file (互換性検証)
[total_tokens u64     ]    server_tokens.tokens.size() at save time (KV との整合性検証)
[n_chunks     u64     ]    map_idx_to_media のエントリ数

[for each chunk:]
  [start_idx  u64     ]    map_idx_to_media のキー (トークン配列上の開始位置)
  [chunk_type u32     ]    MTMD_INPUT_CHUNK_TYPE_TEXT(0) / IMAGE(1) / AUDIO(2)
  [chunk_size u64     ]    後続の chunk-specific data のバイト数
  [chunk-specific data]

[footer_magic u8[4]   ] = "DTMD"  (終端マーカー、ファイル末尾検証用)
```

各 chunk type のデータ (Phase 1 保守版):

| Type | フィールド |
|---|---|
| TEXT | `[n_tokens_text u64] [llama_token[n_tokens_text]]` |
| IMAGE | `[nx u32] [ny u32] [pos_type u32] [image_idx u32] [id_len u64] [id chars] [batch_f32 serialize]` |
| AUDIO | `[n_tokens u32] [id_len u64] [id chars] [batch_f32 serialize]` |

`batch_f32` (`clip_image_f32_batch`) のシリアライズ形式:
```
[is_audio    u8       ]
[grid_x      i32      ]
[grid_y      i32      ]
[n_entries   u64      ]
[for each entry (clip_image_f32):]
  [nx       i32       ]
  [ny       i32       ]
  [add_viewsep u8      ]  // v3
  [add_newline u8      ]  // v3
  [buf_size u64       ]
  [buf      float[]   ]  // image: nx*ny*3, audio: nx*ny
```

ファイルサイズ目安:
- TEXT chunk: 数十バイト (テキストトークンのみ)
- IMAGE chunk: 数百 KB 〜 数 MB (`batch_f32` 込み、§8-3 参照)
- AUDIO chunk: 数百 KB 〜 MB (同様)

**Phase 2.5 縮小最適化候補**: 動作確認後、「`batch_f32` を抜いて動くか」検証して、抜けるなら縮小版に切り替える (§8-3 参照)。

## 6. API 設計

### 6.1 mtmd 層 (`tools/mtmd/mtmd.h` + `mtmd.cpp`)

opaque な `mtmd_input_chunk` のシリアライザを追加。

```cpp
// mtmd.h に追加
MTMD_API size_t mtmd_input_chunk_serialized_size(const mtmd_input_chunk * chunk);
MTMD_API size_t mtmd_input_chunk_serialize(const mtmd_input_chunk * chunk,
                                            uint8_t * buf, size_t buf_size);
MTMD_API mtmd_input_chunk * mtmd_input_chunk_deserialize(const uint8_t * buf,
                                                          size_t buf_size,
                                                          size_t * bytes_read);
```

実装は `mtmd.cpp` 内 (`mtmd_input_chunk` の内部構造にアクセスできる位置)。

### 6.2 server_tokens 層 (`tools/server/server-common.h` + `.cpp`)

```cpp
// struct server_tokens に追加
bool has_media_chunks() const { return !map_idx_to_media.empty(); }

size_t save_mtmd_sidecar(const std::string & filepath,
                          const std::vector<uint8_t> & mmproj_hash) const;
bool load_mtmd_sidecar(const std::string & filepath,
                       const std::vector<uint8_t> & expected_mmproj_hash);
```

### 6.3 server ハンドラ (`tools/server/server-context.cpp`)

```cpp
// check_no_mtmd ガードは削除 (§8-5 参照)

case SERVER_TASK_TYPE_SLOT_SAVE: {
    // 既存: KV キャッシュ保存
    const llama_tokens & tokens = slot->prompt.tokens.get_tokens();
    const size_t nwrite_kv = llama_state_seq_save_file(ctx, filepath.c_str(),
                                                        slot->id,
                                                        tokens.data(),
                                                        token_count);
    // 新規: mmproj 使用かつ media chunks ありの場合 sidecar 保存
    size_t nwrite_mtmd = 0;
    if (mctx && slot->prompt.tokens.has_media_chunks()) {
        std::string mtmd_filepath = filepath + ".mtmd";
        nwrite_mtmd = slot->prompt.tokens.save_mtmd_sidecar(mtmd_filepath, mmproj_hash);
    }
    // レスポンス n_bytes = nwrite_kv + nwrite_mtmd
}

case SERVER_TASK_TYPE_SLOT_RESTORE: {
    // 既存: KV キャッシュ復元
    size_t nread_kv = llama_state_seq_load_file(...);
    // 新規: sidecar 存在チェック → あれば mtmd 復元
    std::string mtmd_filepath = filepath + ".mtmd";
    if (std::filesystem::exists(mtmd_filepath)) {
        if (!mctx) {
            // mmproj 無しでマルチモーダルキャッシュを復元しようとしている
            // → 拒否 (§2-4 不変条件)
            send_error(task, "Cannot restore multimodal cache without mmproj",
                       ERROR_TYPE_INVALID_REQUEST);
            break;
        }
        slot->prompt.tokens.load_mtmd_sidecar(mtmd_filepath, mmproj_hash);
    }
}

case SERVER_TASK_TYPE_SLOT_ERASE: {
    // 既存: prompt_clear のみ。ファイル削除はしない既存挙動を維持
    // (sidecar も同様に erase API では削除しない)
}
```

## 7. 後方互換性マトリクス

| 保存時 | 復元時 | sidecar 有無 | 結果 |
|---|---|---|---|
| mmproj 無し | mmproj 無し | 無し | 既存通り動作 ✅ |
| mmproj 無し | mmproj 有り | 無し | KV のみ復元、`server_tokens.map_idx_to_media` 空 ✅ |
| mmproj 有り (text-only 会話) | mmproj 有り | 無し | KV のみ復元 (sidecar は has_media_chunks() == false で生成しない) ✅ |
| mmproj 有り (画像/音声含) | mmproj 有り (同 mmproj) | 有り | KV + sidecar 復元、map_idx_to_media 完全復元 ✅ |
| mmproj A | mmproj B | 有り | `mmproj_hash` 不一致 → エラー応答 |
| mmproj 有り | mmproj 無し | 有り | エラー応答 (§2-4 不変条件) |

## 8. 設計上の罠 (要レビュー / 未解決事項)

### 8-1. mmproj_hash の取得場所

llama-server が起動時に mmproj ファイルからハッシュを計算する処理は現状無さそう (要確認)。
- `tools/server/server-context.cpp` の起動シーケンスに SHA-256 計算を追加する必要あり
- ハッシュは `server_context` メンバとして保持し、save/load 時に渡す

### 8-2. mtmd opaque 型のシリアライズ責任

`mtmd_image_tokens` / `mtmd_audio_tokens` の構造は `mtmd.cpp` 内部の private。serialize/deserialize は mtmd.cpp 内に書く必要がある。enum 値 `pos_type` (NORMAL/MROPE/HUNYUANVL) を将来変えるとファイル形式互換が壊れるので、`version` field と組み合わせて互換性管理する。

### 8-3. `batch_f32` を保存するか (画像/音声 両方共通)

`mtmd_image_tokens.batch_f32` / `mtmd_audio_tokens.batch_f32` (型: `clip_image_f32_batch`) は前処理済みデータで大きい (画像 1 枚で数百KB〜MB、音声も同程度)。

**仮説**: KV キャッシュに埋め込みベクトルが既に展開されているなら、復元時に `batch_f32` を再エンコードする必要はないはず。メタデータのみ保存すれば足りる。

**しかし**: `mtmd_image_tokens.clone()` は `batch_f32` も clone している (`mtmd.cpp:58-67`)。これは encode 後も保持していることを示唆。コンテキストシフト等で**再 encode が必要なシナリオがある可能性がある**。

**判断**: 初期実装 (Phase 1) は **画像/音声両方とも `batch_f32` を保守的に保存する**版で進める。動作確認後、Phase 2.5 として「`batch_f32` を抜いて再 encode シナリオでも動くか」検証し、抜けるなら縮小版に切り替える。

**ストレージ影響の見立て**: SAIVerse のペルソナごとに画像 N 枚のキャッシュを持つ場合、`N × 数 MB` がディスク消費になる。10 ペルソナ × 5 画像 × 2MB = 100MB 程度。許容範囲だが、長期的には縮小最適化したい。

### 8-4. pos_type ごとの座標復元

NORMAL/MROPE/HUNYUANVL でレイアウトが異なる:
- NORMAL: 1トークン = 1 position
- MROPE: 画像 1 つで `max(t, h, w)` 個の position を消費
- HUNYUANVL: BOI/EOI/newline 含めて `(nx + 1) * ny + 2` 個のトークン

復元時に `n_past` `pos_next` が正しく計算されるか、各 pos_type で検証必要。

### 8-5. `check_no_mtmd()` ガードの扱い

現状 SLOT_SAVE/RESTORE/ERASE 3箇所で呼ばれている。**完全削除**で進める。21133 起票者の方針 (`has_mtmd` フラグを `has_media_chunks()` に置き換え) と整合する。

### 8-6. kaetemi の chunk-based API 提案との関係

issue #19466 で kaetemi が提案した `llama_state_seq_save_file_chunk_*` 系の chunk-based API は、より大規模な設計変更 (`llama_state_seq_save_file()` の deprecate を含む)。

今回の fork 実装では **採用しない** (スコープ過大、upstream 追従性も悪化)。

### 8-7. Hybrid / Recurrent attention モデルでは KV cache 再利用が効かない (llama.cpp 本体の制約)

llama.cpp の `llama_state_seq_save_file` / `load_file` は **recurrent memory** (Gated DeltaNet, Mamba 系, RWKV 系などの hybrid/recurrent attention 構造) を完全には保存・復元できない。具体例:

- **Qwen3.6-35B-A3B** (`10 × (3 × Gated DeltaNet → MoE) → 1 × (Gated Attention → MoE)` のハイブリッド構造)
- 同種の hybrid/recurrent attention モデル全般

これらのモデルで本 fork を使った場合:
- restore リクエスト自体は **成功** し、`.bin` + `.bin.mtmd` の両方とも正しく読み込まれる
- しかし推論時に llama-server 側で `forcing full prompt re-processing due to lack of cache data (likely due to SWA or hybrid/recurrent memory)` と判定され、結局 cold start と同じ時間がかかる

これは **llama.cpp 本体の制約** であり、本 fork のスコープ外。本 fork は upstream の cache save/load 機能をマルチモーダル対応に拡張するだけで、recurrent state の不完全な保存問題は解決しない。

**実用上の判断**:
- 通常 attention モデル (Gemma, Llama, Qwen2.5-VL 等) では本 fork の恩恵が得られる (45x speedup を Gemma 4 で実測)
- hybrid/recurrent モデルでは cache restore が効かないので、本 fork を使う実利は薄い

検証実績 (2026-05-11):
- ✅ Gemma 4 E4B-it (NORMAL pos_type, 通常 attention): 9127ms → 204ms (~45x)
- ⚠️ Qwen 3.6-35B-A3B (hybrid attention): restore 自体は成功するが full re-processing 強制

TurboQuant 移植後の実機検証 (2026-07-23):
- モデル: `gemma-4-E4B-it-UD-Q4_K_XL.gguf` + `mmproj-F32.gguf`
- GPU: NVIDIA GeForce RTX 3090
- 初回: `cache_n=0`, `prompt_n=73`, 画像 encode 実行
- save: `.bin` + `.bin.mtmd` + `.bin.ckpt` を生成
- erase/restore: `n_restored=88`, `.bin + .mtmd` の `n_read=6,404,284` bytes
- server 完全再起動後: checkpoint 3件を disk sidecar から復元
- 再推論: `cache_n=68`, `prompt_n=5`, 画像 encode なし
- 結果: TurboQuant checkpoint と multimodal slot cache の永続化が同居し、プロセスを跨いで画像 encode を省略できることを確認

参考: ggml-org/llama.cpp PR #13194 ("kv-cache: add SWA support") のコメント欄。

## 9. テスト戦略

### 9.1 ユニットテスト (`tools/server/tests/unit/`)

- `test_slot_save_multimodal.py` 新規追加 (fork 内に追加、upstream 提出はしない):
  - mmproj 起動 → 画像 + テキスト投入 → save → restore → 同じ応答が返るか
  - mmproj 起動 → text-only 会話 → save → restore → sidecar 無し
  - mmproj A で保存 → mmproj B で restore → エラー
  - mmproj 起動で保存 → mmproj 無しサーバーで restore → エラー

### 9.2 pos_type ごとの実機検証

| pos_type | 検証モデル | 優先度 |
|---|---|---|
| NORMAL | Gemma 4 E4B-it + mmproj-F32 (まはー所持) | 必須 |
| MROPE | Qwen 2.5-VL 系列 (まはーが調達中) | 必須 |
| HUNYUANVL | 対応モデル不明 | 任意 (fork なので飛ばしてOK、将来 upstream 提案時に追加) |

### 9.3 SAIVerse 統合テスト

fork ビルドの `llama-server.exe` で SAIVerse を起動し、画像入りペルソナ会話でキャッシュが効くこと (2回目以降の推論で prompt eval time が大幅短縮) を確認。

## 10. 進行計画

1. ✅ **fork 準備**: `temp/llama-fork/feature/multimodal-slot-save` ブランチ
2. ✅ **ビルド検証**: CUDA ビルド 4分57秒
3. ✅ **再現確認**: SAIVerse から save → HTTP 501 "not supported by multimodal"
4. ✅ **本ドキュメント レビュー** (設計確定)
5. ✅ **実装 (Phase 1)**: `mtmd_input_chunk` v2/v3 シリアライザ (mtmd.cpp/.h)
6. ✅ **実装 (Phase 2)**: `server_tokens::save/load_mtmd_sidecar()`
7. ✅ **実装 (Phase 3)**: SLOT_SAVE/RESTORE ハンドラ修正、`check_no_mtmd` 削除、mmproj_hash 計算追加
8. **テスト追加**: ✅ model-independent unit / ⬜ pos_type 実機検証 (NORMAL + MROPE)
9. **SAIVerse 統合テスト**: fork ビルドで実環境確認、キャッシュ効果計測
10. **fork のタグ付きリリース**: `v0.1-saiverse-mmcache` 的なタグでビルド成果物公開
11. **SAIVerse README 案内更新**: 「マルチモーダルキャッシュ使う場合は fork 版を」セクション追加
12. **(任意) upstream 提案検討**: 動作実績ができた後、必要なら issue 経由で upstream への提案を検討する。その場合は AGENTS.md に従い**人間主導で書き直す**

## 11. 関連リンク

- upstream issue #21133 (open): https://github.com/ggml-org/llama.cpp/issues/21133
- upstream issue #19466 (closed/stale): https://github.com/ggml-org/llama.cpp/issues/19466
- 関連 merged PR #19849 (内部 context checkpoint): https://github.com/ggml-org/llama.cpp/pull/19849
- fork: https://github.com/maha0525/llama.cpp
- 監視ルーチン: https://claude.ai/code/routines/trig_01CCuAfztsH55kUFCgBHfTo4

## 12. ガバナンス上の判断記録

`temp/llama-fork/AGENTS.md` (upstream `ggml-org/llama.cpp` の AGENTS.md) は AI 主導の PR を明示的に拒否している。具体的には:
- コードの majority が人間によるものであること
- 投稿者が AI 補助なしに全てのコードを説明できること
- 投稿者が AI 補助なしにレビュアー対応とメンテができること

これらの条件は、私 (エア) が主導で実装する形式では満たせない。一方で **Private forks are exempt** と明記されているため、fork 単独利用は対象外。

そのため当面 upstream PR は投げず、SAIVerse 用 fork を公開する形を取る。将来 upstream に提案する場合は、まはーが llama.cpp 内部を学習した上で**書き直す**（または別のコントリビュータが興味を持ったタイミングで託す）。

この判断は 2026-05-11 に確定。

## 13. fork の運用 (2026-08-22 確立)

### ブランチは固定名の一本

主線は **`saiverse/main`** だけ。名前に日付もハッシュも入れない。TurboQuant (`TheTom/llama-cpp-turboquant` の `feature/turboquant-kv-cache`) を取り込むときは、このブランチを rebase する。ブランチ名は変えない。

時点は名前ではなくタグで残す。

- `base/turboquant-<取り込み日>-<TurboQuant 側の短縮ハッシュ>` — どの時点の TurboQuant を土台にしたか
- `backup/pre-rebase-<日付>` — rebase 直前の状態。取り込みに失敗したときの戻り先

健全性の目安は「TurboQuant の主線 + 自作コミット2個」であること。`git rev-list --left-right --count turboquant/feature/turboquant-kv-cache...saiverse/main` が `0 2` を返せば正常で、左が 0 でなければ取り込み漏れ、右が増えていれば差分が育っている。

**docs にコミットハッシュを書かない。** rebase のたびに必ず変わる。指すときはブランチ名とコミット題名で指す。

なぜこうしたか: 2026-08-11 まで `update/turboquant-<日付>` という命名だったため、取り込みのたびにブランチ名が変わり、docs の参照が毎回腐っていた。実際この intent と `docs/issues/llama_prompt_cache_full_reprocess_swa_checkpoint.md` は、どちらも既に使われていないブランチ名と rebase 前のコミットハッシュを指していた。

### GitHub 上の「fork 元」表示は付け替えない

`maha0525/llama.cpp` は GitHub 上では `ggml-org/llama.cpp` からの fork として登録されているが、中身は TurboQuant の系統で、本家とは 144 コミット離れている (2026-08-22 時点)。この表示は実態と食い違うが、付け替えない。

fork 元の表示が実務で効くのは PR の送り先が既定になることだけで、upstream にも TurboQuant にも PR を投げない方針 (§12) では効く場面がない。一方で付け替えれば既存のブランチ・タグ・issue の履歴を失う。デフォルトブランチを `master` から `saiverse/main` に変えたことで、リポジトリを開いたときに実態が見えるようにはなっている。

### upstream の動向 (2026-08-22 時点)

upstream が multimodal の slot save/restore に近い変更を二つ入れた。**どちらも本 fork を不要にはしない。**

1. `server : allow text-only slot save/restore with mtmd (#25076)` — 旧 `check_no_mtmd()` が `check_slot_no_media()` に置き換わり、`--mmproj` 付きで起動していても**その会話に画像や音声が入っていなければ** save/restore を許すようになった。画像が入った会話は今も拒否される。save ハンドラも `get_text_tokens()` しか保存しない。§1 に書いた「テキスト専用ペルソナだけを救う対応では意味がない」がそのまま当てはまる。
2. `mtmd: add chunk save/load function (#26645)` — `mtmd_input_chunk_save()` / `mtmd_input_chunk_load()` が mtmd 層に入った。ただし server 側とは接続されておらず、slot save/restore ハンドラからは呼ばれていない。

2 は本 fork の `mtmd_input_chunk_serialize/deserialize` と役割が重なる。将来この upstream 部品の上に自作分を載せ替えれば、fork の差分を減らせる余地がある (§2 不変条件5 に沿う)。現時点では両者が並存している。

ガードのリネーム (`check_no_mtmd` → `check_slot_no_media`) には対応済みで、fork はリネーム後の名前を外している。
