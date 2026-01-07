# llama.cpp統合ガイド

SAIVerseは、llama.cppを使ってオープンウェイトモデル（GGUF形式）を直接実行できるようになりました。Ollamaのような外部サーバーを立ち上げる必要はありません。

## セットアップ

### 1. llama-cpp-pythonのインストール

```bash
pip install llama-cpp-python
```

GPUアクセラレーション（CUDA）を使う場合：

```bash
CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python
```

Metal（Mac）の場合：

```bash
CMAKE_ARGS="-DGGML_METAL=on" pip install llama-cpp-python
```

### 2. GGUFモデルのダウンロード

Hugging Faceなどから、お好みのGGUFモデルをダウンロードしてください。

推奨モデル（例）：

- **Llama 3 8B Instruct Q8**: 汎用的で高性能
  - https://huggingface.co/QuantFactory/Meta-Llama-3-8B-Instruct-GGUF

- **Qwen2.5 7B Instruct Q8**: 多言語対応、日本語が得意
  - https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF

- **Gemma 2 9B Instruct Q8**: Googleの効率的なモデル
  - https://huggingface.co/google/gemma-2-9b-it-GGUF

ダウンロードしたGGUFファイルを、`~/models/` などの任意のディレクトリに配置します。

### 3. モデル設定の作成/編集

`models/` ディレクトリにある既存の設定ファイルを編集するか、新しく作成します。

例：`models/llama-3-8b-instruct-q8.json`

```json
{
  "model": "llama-3-8b-instruct-q8",
  "display_name": "Llama 3 8B Instruct Q8 (llama.cpp)",
  "provider": "llama_cpp",
  "context_length": 8192,
  "model_path": "~/models/llama-3-8b-instruct-q8_0.gguf",
  "n_gpu_layers": -1,
  "fallback_on_error": true,
  "supports_images": false,
  "parameters": {
    "temperature": 0.7,
    "top_p": 0.9,
    "max_tokens": 2048
  }
}
```

#### 設定項目の説明

- **model**: 設定ファイルのID（ファイル名と一致させることを推奨）
- **display_name**: UIに表示される名前
- **provider**: `"llama_cpp"` 固定
- **context_length**: コンテキスト長（モデルの仕様に合わせる）
- **model_path**: GGUFファイルへのパス（`~` や環境変数が使えます）
- **n_gpu_layers**: GPUにオフロードするレイヤー数
  - `-1`: すべてのレイヤーをGPUで実行（推奨）
  - `0`: CPUのみで実行
  - `32`など正の数: 指定したレイヤー数だけGPUで実行
- **fallback_on_error**: モデルのロード失敗時にGemini 2.0 Flashにフォールバックするか（デフォルト: `true`）
- **supports_images**: 画像入力のサポート（現在llama.cppクライアントでは未対応、`false`）
- **parameters**: デフォルトの生成パラメータ
  - **temperature**: 生成のランダム性（0.0～2.0、デフォルト: 0.7）
  - **top_p**: nucleus sampling（0.0～1.0、デフォルト: 0.9）
  - **max_tokens**: 最大生成トークン数（デフォルト: 2048）

### 4. SAIVerseで使用

SAIVerseを再起動すると、World Editorのモデル選択ドロップダウンに新しいモデルが表示されます。

ペルソナのデフォルトモデル、または軽量モデル（Lightweight Model）として設定できます。

## トラブルシューティング

### モデルが読み込めない

- **GGUFファイルのパスを確認**: `model_path` が正しいか確認してください（`~` は自動展開されます）
- **ファイルの存在確認**: `ls -lh ~/models/` などで実際にファイルがあるか確認
- **ログを確認**: `saiverse_log.txt` に詳細なエラーが出力されます

### メモリ不足エラー

- **量子化レベルを下げる**: Q8の代わりにQ5やQ4のモデルを使用
- **n_gpu_layersを調整**: すべてをGPUに載せられない場合、`32` や `24` など部分的にオフロード
- **context_lengthを短く**: より小さい値（例: 4096）に設定

### GPU認識されない

- **llama-cpp-pythonの再インストール**: CUDA/Metalフラグを付けて再インストール
  ```bash
  pip uninstall llama-cpp-python
  CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python
  ```

### フォールバックが動作しない

- **Gemini APIキーを確認**: `.env` ファイルに `GEMINI_API_KEY` が設定されているか確認
- **fallback_on_errorを有効化**: モデル設定で `"fallback_on_error": true` になっているか確認

## 推奨モデルと用途

| モデル | サイズ | 用途 | GPU推奨 |
|--------|--------|------|---------|
| Llama 3 8B Q8 | ~8GB | 汎用会話、推論 | 8GB VRAM |
| Qwen2.5 7B Q8 | ~7GB | 日本語会話、多言語 | 8GB VRAM |
| Gemma 2 9B Q8 | ~9GB | 効率的な推論 | 10GB VRAM |
| Llama 3 8B Q4 | ~4.5GB | 軽量版（GPUなし向け） | 不要（CPUでも可） |
| Qwen2.5 7B Q4 | ~4GB | 軽量版（GPUなし向け） | 不要（CPUでも可） |

## パフォーマンスチューニング

- **n_gpu_layers**: GPUメモリに余裕がある場合は `-1`（全レイヤー）、メモリが足りない場合は段階的に減らす
- **context_length**: 長い会話履歴が必要な場合は大きく、応答速度を優先する場合は小さく
- **量子化レベル**: Q8 > Q6 > Q5 > Q4 の順に品質が高いが、メモリ消費も大きい

## 既知の制限

- **画像入力**: 現在のllama.cppクライアントは画像入力に未対応（今後対応予定）
- **ツール呼び出し**: モデルによってはツール呼び出しの精度が低い場合があります
- **初回起動が遅い**: モデルのロードに数秒～数十秒かかる場合があります（メモリに読み込まれた後は高速）

## 参考リンク

- [llama.cpp公式リポジトリ](https://github.com/ggerganov/llama.cpp)
- [llama-cpp-python公式ドキュメント](https://llama-cpp-python.readthedocs.io/)
- [Hugging Face GGUFモデル検索](https://huggingface.co/models?library=gguf)
