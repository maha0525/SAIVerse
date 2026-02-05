# GPU セットアップガイド

SAIVerseのEmbedding処理（SAIMemory）でGPU (CUDA) を使用するための設定手順です。

GPUを使用すると、記憶検索（Memory Recall）の埋め込み計算が高速化されます。

## 目次

- [対応環境](#対応環境)
- [Windows Native](#windows-native)
- [WSL2 (Windows Subsystem for Linux)](#wsl2)
- [環境変数での制御](#環境変数での制御)
- [動作確認](#動作確認)
- [トラブルシューティング](#トラブルシューティング)

## 対応環境

| 環境 | GPU | 状況 |
|------|-----|------|
| Windows Native | NVIDIA (CUDA) | ✅ 対応 |
| WSL2 | NVIDIA (CUDA) | ✅ 対応 |
| Linux | NVIDIA (CUDA) | ✅ 対応 |
| macOS | Apple Silicon (Metal) | ⚠️ 未対応（将来対応予定） |
| macOS | Intel | ❌ 非対応 |

## Windows Native

### 前提条件

- NVIDIA GPU（Compute Capability 5.0以上）
- Windows 10/11
- 最新のNVIDIAドライバ

### 手順

#### 1. CUDA Toolkit のインストール

[NVIDIA CUDA Toolkit](https://developer.nvidia.com/cuda-downloads) から Windows 版をダウンロードしてインストール。

推奨バージョン: **CUDA 12.x**

インストール後、コマンドプロンプトで確認:

```cmd
nvcc --version
```

#### 2. cuDNN のインストール

[NVIDIA cuDNN](https://developer.nvidia.com/cudnn) からダウンロード（NVIDIAアカウント必要）。

推奨バージョン: **cuDNN 9.x** (onnxruntime-gpu 1.21+ が要求)

ダウンロードしたzipを展開し、以下のファイルをCUDAインストールディレクトリにコピー:

```
cudnn-windows-x86_64-9.x.x.x_cudaXX-archive/
├── bin/     → C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.x\bin\
├── include/ → C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.x\include\
└── lib/     → C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.x\lib\x64\
```

#### 3. onnxruntime-gpu のインストール

```bash
# 既存のonnxruntimeをアンインストール
pip uninstall onnxruntime -y

# GPU版をインストール
pip install onnxruntime-gpu
```

または requirements-gpu.txt を使用:

```bash
pip install -r requirements-gpu.txt
```

#### 4. 環境変数の設定（必要に応じて）

通常はインストーラが自動設定しますが、問題がある場合は以下を確認:

```
CUDA_PATH = C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.x
Path に追加: %CUDA_PATH%\bin
```

## WSL2

WSL2 では Windows 側のNVIDIAドライバを経由してGPUにアクセスします。

### 前提条件

- Windows 側に NVIDIA GPU ドライバがインストール済み
- WSL2 Ubuntu 環境
- `nvidia-smi` コマンドが動作すること

```bash
nvidia-smi
```

### 手順

#### 1. CUDA Toolkit のインストール

**重要**: WSL2 では `cuda` や `cuda-drivers` メタパッケージをインストールしないでください。`cuda-toolkit` のみをインストールします。

```bash
# NVIDIA CUDA リポジトリの追加
wget https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt-get update

# CUDA Toolkit のインストール
sudo apt-get install -y cuda-toolkit-12-6
```

#### 2. cuDNN 9 のインストール

onnxruntime-gpu は cuDNN 9 を要求します。

```bash
# Ubuntu 24.04 の場合
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt-get update

# cuDNN のインストール
sudo apt-get install -y cudnn
```

##### トラブルシューティング: パッケージ競合

既存の `nvidia-cudnn` (cuDNN 8) がインストールされている場合、競合が発生することがあります。

```bash
# 既存の nvidia-cudnn を削除
sudo apt-get remove -y nvidia-cudnn

# 依存関係を修復
sudo apt --fix-broken install
```

#### 3. onnxruntime-gpu のインストール

```bash
pip uninstall onnxruntime -y
pip install onnxruntime-gpu
```

#### 4. 環境変数の設定

```bash
echo 'export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
source ~/.bashrc
```

## 環境変数での制御

`.env` ファイルでGPU/CPUを明示的に切り替えられます:

```bash
# GPU を使用（CUDAが利用可能な場合）
SAIMEMORY_EMBED_CUDA=1

# CPU を使用（GPUが利用できない環境、またはGPUを使いたくない場合）
SAIMEMORY_EMBED_CUDA=0
```

**未設定の場合**: 自動検出されます（CUDAExecutionProvider が利用可能なら GPU を使用）。

## 動作確認

### ONNX Runtime のプロバイダー確認

```bash
python -c "import onnxruntime as ort; print('Available providers:', ort.get_available_providers())"
```

**GPU が使用可能な場合の出力例**:

```
Available providers: ['TensorrtExecutionProvider', 'CUDAExecutionProvider', 'CPUExecutionProvider']
```

**CPU のみの場合の出力例**:

```
Available providers: ['AzureExecutionProvider', 'CPUExecutionProvider']
```

### SAIVerse での確認

SAIVerse 起動時のログを確認:

```
# GPU使用時 - 警告なし
2024-xx-xx [INFO] sai_memory.memory.recall: Using CUDA for embeddings

# CPU使用時 - 警告が出る（正常動作）
2024-xx-xx [WARNING] sai_memory.memory.recall: CUDA initialization failed ...; falling back to CPU.
```

## トラブルシューティング

### `CUDAExecutionProvider is not available`

**原因**: onnxruntime-gpu がインストールされていない、または CPU 版の onnxruntime がインストールされている。

**解決策**:

```bash
pip uninstall onnxruntime onnxruntime-gpu -y
pip install onnxruntime-gpu
```

### `libcudnn.so.9: cannot open shared object file` (Linux/WSL2)

**原因**: cuDNN 9 がインストールされていない、またはライブラリパスが通っていない。

**解決策**:

```bash
# cuDNN のインストール状況を確認
find /usr -name "libcudnn.so*" 2>/dev/null

# 見つからない場合は cuDNN をインストール
sudo apt-get install -y cudnn
```

### `libcublasLt.so.12: cannot open shared object file` (Linux/WSL2)

**原因**: CUDA Toolkit がインストールされていない。

**解決策**: 上記の「CUDA Toolkit のインストール」を実行。

### `Failed to create CUDAExecutionProvider`

**原因**: cuDNN のバージョンが合っていない。onnxruntime-gpu 1.21+ は cuDNN 9 を要求。

**解決策**: cuDNN 9 をインストールし、古いバージョンを削除。

### Windows で DLL が見つからない

**原因**: CUDA/cuDNN の DLL にパスが通っていない。

**解決策**:

1. システム環境変数で `CUDA_PATH` が設定されているか確認
2. `%CUDA_PATH%\bin` が `Path` に含まれているか確認
3. PC を再起動

### GPU はあるが CPU にフォールバックする

**原因**: 環境変数 `SAIMEMORY_EMBED_CUDA=0` が設定されている可能性。

**解決策**: `.env` ファイルを確認し、`SAIMEMORY_EMBED_CUDA=1` に設定するか、行を削除して自動検出に任せる。

## 参考リンク

- [NVIDIA CUDA Toolkit](https://developer.nvidia.com/cuda-toolkit)
- [NVIDIA cuDNN](https://developer.nvidia.com/cudnn)
- [CUDA on WSL User Guide](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)
- [ONNX Runtime CUDA Execution Provider](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)
