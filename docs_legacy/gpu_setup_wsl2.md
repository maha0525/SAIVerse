# WSL2 で GPU を使った Embedding を有効化する

SAIMemory の embedding 処理で GPU (CUDA) を使用するための設定手順です。

## 前提条件

- Windows 側に NVIDIA GPU ドライバがインストール済み
- WSL2 Ubuntu 環境
- `nvidia-smi` コマンドが動作すること

```bash
nvidia-smi
```

## 手順

### 1. CUDA Toolkit のインストール

**重要**: WSL2 では `cuda` や `cuda-drivers` メタパッケージをインストールしないでください。`cuda-toolkit` のみをインストールします。

```bash
# NVIDIA CUDA リポジトリの追加
wget https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt-get update

# CUDA Toolkit のインストール
sudo apt-get install -y cuda-toolkit-12-6
```

### 2. cuDNN 9 のインストール

onnxruntime-gpu は cuDNN 9 を要求します。

```bash
# Ubuntu 24.04 の場合、NVIDIA のリポジトリを追加
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt-get update

# cuDNN のインストール
sudo apt-get install -y cudnn
```

#### トラブルシューティング: パッケージ競合

既存の `nvidia-cudnn` (cuDNN 8) がインストールされている場合、競合が発生することがあります。

```bash
# 既存の nvidia-cudnn を削除
sudo apt-get remove -y nvidia-cudnn

# 依存関係を修復
sudo apt --fix-broken install
```

それでもエラーが出る場合:

```bash
# 強制上書き
sudo dpkg --force-overwrite -i /var/cache/apt/archives/libcudnn9-dev-cuda-13_*.deb

# パッケージ設定を完了
sudo dpkg --configure -a
sudo apt --fix-broken install
```

### 3. onnxruntime-gpu のインストール

```bash
pip uninstall onnxruntime -y
pip install onnxruntime-gpu
```

### 4. 環境変数の設定 (オプション)

ライブラリパスを設定:

```bash
echo 'export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
source ~/.bashrc
```

## SAIMemory での GPU 使用

### 環境変数での制御

`.env` ファイルで GPU/CPU を切り替えられます:

```bash
# GPU を使用
SAIMEMORY_EMBED_CUDA=1

# CPU を使用 (GPU が利用できない環境向け)
SAIMEMORY_EMBED_CUDA=0
```

未設定の場合は自動検出されます (CUDAExecutionProvider が利用可能なら GPU を使用)。

### 動作確認

```bash
python -c "import onnxruntime as ort; print('Available providers:', ort.get_available_providers())"
```

出力に `CUDAExecutionProvider` が含まれていれば GPU が使用可能です:

```
Available providers: ['TensorrtExecutionProvider', 'CUDAExecutionProvider', 'CPUExecutionProvider']
```

## よくあるエラー

### `libcudnn.so.9: cannot open shared object file`

cuDNN 9 がインストールされていない、またはライブラリパスが通っていません。

```bash
# cuDNN のインストール状況を確認
find /usr -name "libcudnn.so*" 2>/dev/null
```

### `libcublasLt.so.12: cannot open shared object file`

CUDA Toolkit がインストールされていません。手順 1 を実行してください。

### `Failed to create CUDAExecutionProvider`

cuDNN のバージョンが合っていない可能性があります。onnxruntime-gpu 1.23+ は cuDNN 9 を要求します。

## 参考リンク

- [CUDA on WSL User Guide](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)
- [NVIDIA cuDNN Installation Guide](https://docs.nvidia.com/deeplearning/cudnn/installation/latest/linux.html)
- [ONNX Runtime CUDA Execution Provider](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)
