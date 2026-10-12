# GPU セットアップガイド (廃止)

**想起用の埋め込みは CPU 固定になったため、この手順は不要です (2026-10-12)。**

この文書は、SAIMemory の埋め込み計算 (記憶検索のために文章をベクトルに変える処理) を GPU (CUDA) で動かすための手順書でした。埋め込みモデルが EmbeddingGemma 2 の 4bit 量子化版に変わったときに、埋め込みは CPU だけで動かす設計になりました。

- 理由: GPU のある環境ほど、ローカル LLM が GPU を使っていることが多い。埋め込みが GPU を取り合いに行かないようにするため。新しいモデルは CPU でも想起 1 回あたり約 30ms で動く。
- `SAIMEMORY_EMBED_CUDA` は廃止され、設定しても読まれません。`.env` に残っていれば消して構いません。
- `onnxruntime-gpu` も不要です。以前この手順で `onnxruntime` を `onnxruntime-gpu` に入れ替えていた場合も、CPU での実行は `onnxruntime-gpu` にも含まれているので、通常はそのままで動きます。元の CPU 版に戻したいときは、両方を外してから `requirements.lock` を入れ直してください (`python -m pip uninstall -y onnxruntime-gpu onnxruntime` の後に `python -m pip install -r requirements.lock`。二つは同じ場所にファイルを置くので、片方だけ外すともう片方が壊れることがある)。

設計の経緯は [docs/intent/embedding_gemma2_migration.md](../intent/embedding_gemma2_migration.md) にあります。旧版の手順 (CUDA Toolkit / cuDNN / onnxruntime-gpu の導入) は git の履歴に残っています。
