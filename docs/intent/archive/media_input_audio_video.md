# Intent Document: ユーザー添付の音声・動画入力対応

**ステータス**: 実装完了 (v1.0, 2026-05-18)
**位置付け**: ユーザーが UI からチャットに添付する音声・動画ファイルと、audio/video タイプのアイテムを、画像と同じ思想でペルソナの LLM 認知に乗せるための入力経路。既存の画像添付経路 (`api/routes/media.py:upload`, `iter_image_media`, `load_image_bytes_for_llm`, `ensure_image_summary`) と picture アイテム経路 (`get_visual_context._render_item` の Open 状態挙動) を音声・動画に拡張する形で実装する。
**前提**: [multimodal_input_pipeline.md](multimodal_input_pipeline.md) (ツール戻り値経由のメディア基盤、v0.1) / [model_provider_management.md](model_provider_management.md) (モデル設定の 3 層優先) / Gemini API 仕様 ([audio](https://ai.google.dev/gemini-api/docs/audio), [video understanding](https://ai.google.dev/gemini-api/docs/video-understanding))

---

## 1. これは何か / 何でないか

### これは何か

まはー (ユーザー) がチャット UI で音声ファイル (mp3/wav/ogg/flac/aac/aiff) や動画ファイル (mp4/mov/webm 等) を添付したとき、それをペルソナの LLM 入力にネイティブメディアとして流す経路。画像で既に動いている経路 (`/api/media/upload-file` → サーバ側で正規化・保存 → metadata.media[] → `iter_*_media` → LLM client が inline 送信、または非対応モデル向けにサマリテキスト注入) を音声・動画に拡張する。

具体的に提供するもの:

- `Item.TYPE` に `"audio"` / `"video"` を追加し、保存先 `~/.saiverse/audio/` `~/.saiverse/video/` を新設
- アップロード時に ffmpeg で **音声は opus 24kbps モノラル 16kHz ogg / 動画は 1FPS + 480p + 音声 24kbps モノラル 16kHz mp4** へ正規化
- 入口での長さ上限を **音声 5 分 / 動画 90 秒** で強制 (超過時はアップロード自体を拒否)
- `media_summary.py` を拡張し `ensure_audio_summary` / `ensure_video_summary` を実装、非対応モデル向けに `*.ogg.summary.txt` / `*.mp4.summary.txt` の sidecar を生成
- ペルソナごとの `AUDIO_MODEL` / `VIDEO_MODEL` 設定欄を追加、unset 時は `SAIVERSE_AUDIO_SUMMARY_MODEL` / `SAIVERSE_VIDEO_SUMMARY_MODEL` 環境変数 → `BUILTIN_DEFAULT_LITE_MODEL` の順にフォールバック
- LLM client 側に `supports_audio` / `supports_video` フラグと、Gemini の inline_data として音声・動画を送る経路
- **アイテム経路の対応**: audio/video タイプのアイテムを Open 状態にすると、`get_visual_context` 経由で毎回 LLM 認知に乗る (picture アイテムの Open 挙動と完全に対称)。`item_view` ツールでも一時的に閲覧できる
- ffmpeg バイナリは `imageio-ffmpeg` を `requirements.txt` に追加して pip install で同梱、ユーザーに個別インストールを促さない

### これは何でないか

- **MCP / ネイティブツール戻り値経由のメディア基盤の設計ではない**。 [multimodal_input_pipeline.md](multimodal_input_pipeline.md) が扱う `MediaBuffer` / `promote_media` / `disposition` 系の設計はそちらの責務。本書は「ユーザー UI 添付」という別の入口からの経路を扱う。両経路は出口側 (`media_summary` / `iter_*_media` / LLM client の inline_data 構築) で合流する
- **動画の長尺対応 (Files API) ではない**。 90 秒上限・インライン送信のみで完結させる方針。1 分ごとに 1 分の動画でモニタリングする用途を想定するため、60 秒ではなく 90 秒に設定する
- **動画ストリーミング入力ではない**。リアルタイム映像 (カメラ等) は別経路 (MCP ImageContent + MediaBuffer) で、本書のスコープ外
- **既存画像経路の置き換えではない**。画像は現状経路を維持。音声・動画は同じ思想の独立経路として並列に追加し、共通化できる部分 (`media_summary.py` の構造、`iter_*_media` のパターン) はリファクタで揃える

---

## 2. なぜ必要か

### 動機 1: マルチモーダルな会話体験の実現

画像添付は既に動いているが、音声・動画はユーザーが直接ペルソナに渡せない。「この音を聞いてみて」「この動画について話そう」というやり取りができないままだと、Gemini の音声/動画理解能力 (1 秒 = 32 token / 300 token、9.5 時間 / 1 時間まで対応) を SAIVerse の対話体験に活かせない。

### 動機 2: 音声 / 動画認識モデルの分離

Gemini は音声・動画をネイティブで理解できるが、Anthropic Claude や OpenAI GPT (画像のみ対応モデルが多い) は理解できない。画像と同様に「認識用モデルで一度サマリを作って、後段の LLM にはテキスト注入する」フォールバック経路が要る。これがないと、ペルソナのデフォルトモデルが音声非対応のとき、ユーザーが添付した音声がペルソナに何も伝わらない事故が起きる。

### 動機 3: ファイルサイズ問題の現実解

Gemini のリクエスト総量は 20MB 固定上限。wav (44.1kHz/16bit ステレオで約 10MB/分) を直送すると 2 分で超過する。Files API を採用すると 48 時間で消える URI の管理、再アップロード経路、quota 計上などの設計負債が出る。

→ **音声 5 分上限 / 動画 90 秒上限 + ffmpeg で正規化** すれば、音声 (opus 24kbps モノラル 16kHz) で最大約 900KB、動画 (1FPS+480p+音声 24kbps) で約 1-3MB に収まり、インライン送信のみで完結する。Files API は採用しない。

ただし、リクエスト 20MB は **会話履歴全体の合計** に効く制約なので、Open 状態の音声/動画アイテムが過剰に乗ったり、長い会話履歴の中で複数の添付メディアが累積するとリクエスト総量が膨らむ。これを抑える設計上の仕掛けは「入口の長さ上限」「24kbps への正規化」「Open/Closed 切替によるペルソナ判断」の 3 段で対処する。

### 動機 4: ユーザー層の現実

SAIVerse のターゲットユーザーには「ffmpeg を別途インストールしてください」が高い障壁。`imageio-ffmpeg` (BSD-2-Clause、各 OS 向けバイナリを pip wheel に同梱) を使えば setup スクリプトに何も追加せず pip install で完結する。これによって `feedback_user_experience_first.md` の方針 (環境固有の障壁は upstream 対処を最優先) を守れる。

---

## 3. 守るべき不変条件

### C1. 入口での正規化を全経路で強制する

ユーザーがアップロードした音声・動画は、保存前に必ず ffmpeg で正規化する。 wav 原本を `~/.saiverse/audio/` に保存して LLM 送信時に変換、のような「保存時は原本、送信時に変換」設計は採用しない。理由は (a) 容量の浪費、(b) LLM 送信ごとの変換コスト、(c) Gemini が内部で 16kbps モノラルにダウンサンプルするので入力品質を上げても無駄。

例外: 既に opus/mp3 32kbps モノラル相当に圧縮済みのファイルは ffmpeg をスキップしてもよい (実装時に判断、最小限の最適化として後追い OK)。

### C2. 長さ上限 (音声 5 分 / 動画 90 秒) は入口で強制する

ffmpeg で metadata 読み取り → 音声 duration > 300s または動画 duration > 90s なら HTTP 400 で拒否。「上限超過したものをトリミングして保存」はしない (ユーザーが意図しない範囲を切られると事故になる)。ユーザーに「5 分 / 90 秒以内に編集してから再アップロードしてください」と明示する。

「5 分」「90 秒」を選んだ理由は §6 を参照。

### C3. サマリ sidecar は永続キャッシュ

`*.ogg.summary.txt` / `*.mp4.summary.txt` は画像と同じく「一度生成したら再利用」する。同じファイルが複数の会話・複数のペルソナで使われても、サマリ生成 LLM 呼び出しは 1 回で済む。`media_summary.py` の `_generating_lock` / `_generating_paths` 再入防止ガードも同じパターンで実装する。

### C4. 非対応モデルでも会話は破綻しない

ペルソナのデフォルトモデルが音声非対応でも、サマリテキストが LLM 入力に注入されるので「ユーザーが何か添付した」事実とその概要は伝わる。LLM client の `supports_audio` / `supports_video` フラグが `False` の場合、media は送らずサマリのみを text に追記する (画像の既存挙動と完全に対称)。

### C5. multimodal_input_pipeline の MediaBuffer と概念衝突しない

ユーザー添付メディアは `MediaBuffer` に登録しない。MediaBuffer は揮発メディア (pulse 単位で消える) のための機構で、ユーザー添付は最初からファイル永続化されている。代わりに metadata.media[] にファイルパス参照を直接入れる (画像と同じパターン)。

将来的に「ユーザーが添付した音声を Item として inventory に入れる」需要が出たら、それは `promote_media` ツール経由ではなく、UI 操作 (添付メッセージ右クリック → アイテム化、等) として別途設計する。

### C6. ffmpeg バイナリの存在を実行時にチェック

`imageio-ffmpeg.get_ffmpeg_exe()` が失敗した場合、音声・動画アップロード経路は HTTP 503 を返す (UI 側で「環境設定が不完全です」と表示)。サイレント失敗しない。

### C7. Open 状態の audio/video アイテムは picture/document と同じ思想で扱う

`get_visual_context._render_item` の picture / document / bag に並ぶ形で audio / video の分岐を追加する。`is_open=True` のとき:

- text に `saiverse://item/<id>/audio` または `saiverse://item/<id>/video` URI を埋め込み
- `media_list` に `{"path": <resolved>, "mime_type": "audio/ogg"|"video/mp4", "type": "audio"|"video"}` を追加
- LLM client 側で対応プロバイダなら inline_data として bytes 送信、非対応なら text 注釈 + サマリ注入

`is_open=False` の場合は description のみ表示 (既存 picture/document と完全に対称)。これによってまはーの想定用例「ユーザーの声 audio を Open で常に持たせて発話者判別」が成立する。Closed のアイテムは「持っているが今は見聞きしていない」状態。

ペルソナが「これは Open する / Closed にする」を判断する責任は picture/document と同じ。本機構が「常に全アイテムを Open」「常に Closed」を強制してはならない。

---

## 4. 設計

### 4.1. データフロー全体図

```
[ユーザーが UI で音声/動画ファイルを添付]
  ↓
[frontend: FileUpload.tsx] MIME 判定で /api/media/upload-audio or /upload-video へ POST
  ↓
[api/routes/media.py] ffmpeg で正規化 (音声 → opus ogg / 動画 → 1FPS+480p mp4)
  ↓ (動画は 90 秒チェック、超過なら 400)
  ~/.saiverse/audio/<timestamp>_<uuid>.ogg
  ~/.saiverse/video/<timestamp>_<uuid>.mp4 に保存
  ↓
レスポンス: {"url": "/api/media/audio/...", "type": "audio"|"video", "relative_path": "audio/..."}
  ↓
[ペルソナへチャット送信時] message.metadata.media[] に {"uri": "saiverse://audio/...", "mime_type": "audio/ogg"} を含める
  ↓
[LLM 呼び出し前] media_summary.ensure_audio_summary / ensure_video_summary がサマリを生成 (初回のみ) し sidecar に保存
  ↓
[llm_clients/*] 各 provider の content block 構築:
  - Gemini で supports_audio=True / supports_video=True: inline_data に bytes を載せる
  - それ以外 (supports_audio=False): media を載せず、text に [音声: <summary>] を追記
```

### 4.2. ffmpeg 正規化スペック

| 種別 | 入力 | 出力 | ffmpeg コマンド (概念) | 長さ上限 |
|---|---|---|---|---|
| 音声 | wav/mp3/aac/flac/aiff/ogg | ogg (libopus, 24kbps, mono, 16kHz) | `-c:a libopus -b:a 24k -ac 1 -ar 16000` | 5 分 |
| 動画 | mp4/mov/webm/avi 等 | mp4 (h264 480p 1FPS + opus 24kbps mono 16kHz) | `-vf scale=-2:480 -r 1 -c:v libx264 -crf 28 -c:a libopus -b:a 24k -ac 1 -ar 16000` | 90 秒 |

**動画のフレームレート 1FPS は Gemini 内部で 1FPS にダウンサンプルされる仕様に揃えた値**。これより高くしてもトークン消費が増えるだけ。低くすると Gemini 側で「フレームが足りない」とは判定されないが、SAIVerse 側で監視映像のような用途を想定すると 1FPS 確保が望ましい。

**動画の音声トラックも 24kbps モノラルに統一**: Gemini は動画内の音声も 1kbps モノラルで処理する。SAIVerse 側で 24kbps モノラルに揃えれば、「会話している動画」を投げてきたケースで発話内容も拾える。音声単体と動画の音声トラックでビットレートを揃えることで、データフローが単純になる。

### 4.3. モデルロール拡張

`database/models.py` の `AI` テーブルに以下のカラムを追加:

```python
AUDIO_MODEL = Column(String, nullable=True)  # 音声認識用モデル
VIDEO_MODEL = Column(String, nullable=True)  # 動画認識用モデル
```

`saiverse/media_summary.py` のフォールバック順:

```
persona.AUDIO_MODEL (DB)
  ↓ unset
SAIVERSE_AUDIO_SUMMARY_MODEL (env)
  ↓ unset
BUILTIN_DEFAULT_LITE_MODEL (= 現状 gemini-2.0-flash 系)
```

video も同様。`VISION_MODEL` / `LIGHTWEIGHT_VISION_MODEL` のような 2 段は設けない (Gemini 以外で音声・動画ネイティブ対応モデルがほぼ存在しないため、軽量版を分ける意味が薄い)。

### 4.4. Item タイプ拡張

`Item.TYPE` は自由文字列のため、enum 化はしない (既存 `"picture"` / `"object"` / `"document"` と同じ運用)。新規追加:

- `"audio"`: 音声アイテム、`FILE_PATH` は `audio/<filename>.ogg`
- `"video"`: 動画アイテム、`FILE_PATH` は `video/<filename>.mp4`

`saiverse/media_utils.py` の URI prefix を拡張:

```python
AUDIO_URI_PREFIX = "saiverse://audio/"
VIDEO_URI_PREFIX = "saiverse://video/"
ITEM_AUDIO_URI_PREFIX = "saiverse://item/<item_id>/audio"
ITEM_VIDEO_URI_PREFIX = "saiverse://item/<item_id>/video"
```

frontend サニタイザ (`feedback` メモの `saiverse:// URI Protocol Handling` 案件) は既に `saiverse:` を許可済みなので追加対応不要。`resolve_extended_media_uri` に audio/video の分岐を追加する。

### 4.5. API エンドポイント

`api/routes/media.py` に追加:

| エンドポイント | 用途 | 正規化 | サイズ/長さ制限 |
|---|---|---|---|
| `POST /upload-audio` | 音声アップロード | opus 24kbps mono 16kHz ogg | duration 5 分、入力ファイルサイズ 100MB |
| `POST /upload-video` | 動画アップロード | 1FPS+480p+opus 24kbps mono mp4 | duration 90 秒、入力ファイルサイズ 500MB |
| `GET /audio/{filename}` | 音声配信 | - | - |
| `GET /video/{filename}` | 動画配信 | - | - |

`POST /upload-file` (auto 判定) を拡張し、`content_type.startswith("audio/")` / `startswith("video/")` の分岐を追加する。

**duration チェックは ffmpeg 正規化と兼ねて行う**: アップロードされたバイト列をいったん一時ファイルに書いて ffprobe (or `ffmpeg -i` の stderr パース) で duration 取得 → 超過なら 400 で拒否、合格なら ffmpeg で正規化して保存。「先に保存して後で削除」は半端なファイルが残るので採用しない。

### 4.6. frontend 拡張

`frontend/src/components/common/FileUpload.tsx` の auto 判定に audio/video 分岐を追加。チャット入力欄から添付できるようにする。プレビューは:

- 音声: `<audio controls>` で再生バー表示
- 動画: `<video controls>` で最初のフレーム + 再生

添付メッセージのバブル表示も同様に audio/video タグで埋め込む。

### 4.7. LLM client 拡張

`llm_clients/base.py` の `LLMClientBase.__init__` に `supports_audio: bool = False`, `supports_video: bool = False` を追加。

`llm_clients/gemini.py` の `_convert_messages` で、metadata.media[] から音声・動画 URI を抽出し、`types.Part.from_bytes(audio_bytes, "audio/ogg")` / `types.Part.from_bytes(video_bytes, "video/mp4")` として inline_data に乗せる。

`metadata.media[]` の各要素には `type` フィールド (`"image"` / `"audio"` / `"video"`) を持たせる。既存の picture 経路は `type` を省略しても `"image"` 扱いで動くようにフォールバックを残す (後方互換)。

`saiverse/media_utils.py` に新規追加:

```python
SUPPORTED_LLM_AUDIO_MIME = {"audio/wav", "audio/mp3", "audio/aiff", "audio/aac", "audio/ogg", "audio/flac"}
SUPPORTED_LLM_VIDEO_MIME = {"video/mp4", "video/mpeg", "video/quicktime", "video/avi", "video/webm", "video/wmv", "video/x-flv", "video/3gpp"}

def iter_audio_media(metadata) -> List[Dict[str, Any]]: ...
def iter_video_media(metadata) -> List[Dict[str, Any]]: ...
def load_audio_bytes_for_llm(path, mime_type) -> Tuple[Optional[bytes], Optional[str]]: ...
def load_video_bytes_for_llm(path, mime_type) -> Tuple[Optional[bytes], Optional[str]]: ...
```

非対応モデル (Anthropic, OpenAI 等) の場合、metadata.media[] から音声・動画を**スキップ**し、`media_summary.ensure_audio_summary` / `ensure_video_summary` の結果を `[音声: <summary>]` / `[動画: <summary>]` として text 末尾に追記する。

### 4.8. アイテム閲覧経路 (Open 状態 + item_view)

#### get_visual_context の audio/video 対応

`builtin_data/tools/get_visual_context.py` の `_render_item` に audio/video の分岐を追加。既存の picture (line 269-293) と document (line 295-320) のパターンを踏襲する:

```python
elif item_type == "audio":
    open_label = "(Open)" if is_open else "(Closed)"
    text_parts.append(f"{ref_label}[Audio] {item_name}")
    text_parts.append(open_label)
    if created_at_str:
        text_parts.append(f"作成日時: {created_at_str}")

    if is_open and file_path_str:
        resolved = _resolve_item_file_path(manager, file_path_str)
        if resolved and os.path.exists(resolved):
            text_parts.append(f"saiverse://item/{item_id}/audio")
            _add_audio_to_media_list(resolved, media_list)
            # description は caption として、サマリは LLM 注釈として後段で追加
            text_parts.append(description)
        else:
            text_parts.append(description)
    else:
        text_parts.append(description)
    text_parts.append("")

elif item_type == "video":
    # 同様に video URI と media_list 追加
    ...
```

`_add_to_media_list` を `_add_image_to_media_list` / `_add_audio_to_media_list` / `_add_video_to_media_list` に分岐させ、`type` フィールドをそれぞれ `"image"` / `"audio"` / `"video"` で書き込む。

#### item_view ツールの audio/video 対応

`builtin_data/tools/item_view.py` は `manager.view_items_for_persona()` に委譲しているだけなので、実体は `manager/items.py` の `ItemService.view_items_for_persona` を audio/video 対応に拡張する:

- 既存: picture → file path 返却、document → text 返却、bag → 中身リスト返却、object → description 返却
- 拡張: audio → ファイル path + 「音声を聞きました」相当の text + media_list 注入、video → 同様

戻り値は現状 `str` だが、媒体を LLM 認知に乗せるには message dict 構造の戻り値が必要。実装方針は 2 つ:

1. **戻り値スキーマを `Tuple[str, List[Dict]]` に拡張** (multimodal_input_pipeline.md の `_format_tool_result` 拡張と歩調を合わせる)
2. **tools/context 経由で current pulse に media を inject する** (副作用ベース、現在の `str` 戻り値は維持)

実装時にどちらが現状の SEA runtime と整合的か検証する。multimodal_input_pipeline.md の Phase 5 以降の方針と一致させたい。

#### Open/Closed 切替

audio/video アイテムの Open/Closed は picture/document と同じく `state.is_open` で表現。既存の `item_use` などのツールで `is_open` を切り替える経路をそのまま使えるか、新規ツールが要るかは実装時に確認する。

### 4.8. ffmpeg バンドル方針

`requirements.txt` に追加:

```
imageio-ffmpeg>=0.6.0
```

`saiverse/ffmpeg_runner.py` (新規) でラッパーを提供:

```python
import imageio_ffmpeg
import subprocess

def get_ffmpeg_path() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()

def run_ffmpeg(args: list[str]) -> bytes:
    """ffmpeg を subprocess で呼んで stdout を返す。失敗時は CalledProcessError"""
    ...

def probe_duration(path: Path) -> float:
    """ffprobe (= ffmpeg -i パース) で動画/音声の長さを取得"""
    ...
```

`imageio-ffmpeg` は ffmpeg 本体のみ同梱で ffprobe はない。duration 取得は `ffmpeg -i <input> -f null -` の stderr パース、または PyAV / mediainfo パッケージ追加で対応。実装時に判断する。

---

## 5. 既存実装からの差分マップ

| ファイル | 変更内容 |
|---|---|
| `requirements.txt` | `imageio-ffmpeg>=0.6.0` 追加 |
| `saiverse/ffmpeg_runner.py` | **新規**: ffmpeg ラッパー (get_ffmpeg_path, run_ffmpeg, probe_duration) |
| `api/routes/media.py` | `POST /upload-audio`, `POST /upload-video`, `GET /audio/{filename}`, `GET /video/{filename}` 追加、`POST /upload-file` の auto 判定拡張 |
| `saiverse/media_utils.py` | `SUPPORTED_LLM_AUDIO_MIME`, `SUPPORTED_LLM_VIDEO_MIME`, `AUDIO_URI_PREFIX`, `VIDEO_URI_PREFIX`, `_ensure_audio_dir`, `_ensure_video_dir`, `resolve_media_uri` 拡張, `iter_audio_media`, `iter_video_media`, `load_audio_bytes_for_llm`, `load_video_bytes_for_llm`, `store_audio_bytes`, `store_video_bytes`, `resolve_extended_media_uri` 拡張 (saiverse://item/<id>/audio, saiverse://item/<id>/video) |
| `saiverse/media_summary.py` | `_AUDIO_SUMMARY_MODEL_RAW`, `_VIDEO_SUMMARY_MODEL_RAW` 定数、`ensure_audio_summary`, `ensure_video_summary`, `_generate_audio_summary`, `_generate_video_summary` 追加。再入防止ガードは画像と共通化 |
| `database/models.py` | `AI.AUDIO_MODEL`, `AI.VIDEO_MODEL` カラム追加 |
| `database/migrate.py` | 上記カラム追加のマイグレーション |
| `llm_clients/base.py` | `supports_audio`, `supports_video` フラグ追加 |
| `llm_clients/gemini.py` | `_convert_messages` で audio/video の inline_data 構築追加、`__init__` で `supports_audio`/`supports_video` 受け取り |
| `builtin_data/models/*.json` | Gemini 系モデルに `"supports_audio": true`, `"supports_video": true` 追加 |
| `saiverse/model_configs.py` | `model_supports_audio`, `model_supports_video` 追加 |
| `saiverse/model_defaults.py` | コメント整理 (lite model が音声・動画サマリにも使われる旨) |
| `frontend/src/components/common/FileUpload.tsx` | audio/video MIME 分岐追加、プレビュー UI |
| `frontend/src/components/chat/*.tsx` | 添付メッセージのバブル表示で audio/video タグ埋め込み |
| `builtin_data/tools/get_visual_context.py` | `_render_item` に audio/video 分岐追加 (Open 状態時に URI と media_list 注入)、`_add_to_media_list` を type 別に分岐 |
| `builtin_data/tools/item_view.py` | コメント更新 (audio/video 対応) |
| `manager/items.py` | `ItemService.view_items_for_persona` の audio/video 対応、戻り値スキーマ拡張 (実装時に判断) |
| `builtin_data/tools/item_use.py` 周辺 | audio/video の Open/Closed 切替に対応するか確認 |

---

## 6. 長さ上限の根拠

### 6.1. 動画 90 秒

| 要素 | 値 |
|---|---|
| Gemini インライン上限 | request 全体 20MB |
| Gemini の動画トークン換算 | デフォルト 300 token/秒 |
| 90 秒動画のトークン | 27,000 token |
| 90 秒動画のサイズ目安 (480p+1FPS+音声 24kbps mono) | 1-3 MB |
| 想定用途 | 「1 分ごとに 1 分の動画を送ってモニタリング」 (60 秒ぴったりはマージン無し) |

90 秒は「1 分用途に対するマージン (+50%)」「インライン 20MB に十分収まる」「コンテキスト 1M token モデルでも 27K token は誤差レベル」の 3 点でバランスが取れた値。

### 6.2. 音声 5 分

| 要素 | 値 |
|---|---|
| Gemini インライン上限 | request 全体 20MB |
| Gemini の音声トークン換算 | 32 token/秒 |
| 5 分音声のトークン | 9,600 token |
| 5 分音声のサイズ (opus 24kbps mono) | 約 900 KB |
| 想定用途 | 短い会話の録音、ボイスメモ、効果音、楽曲のサビ部分など |
| マージン考慮 | 「会話履歴全体」が 20MB 制約。5 分音声を毎メッセージに添付しても会話 20 ターンで 18MB 程度に収まる |

技術的には opus 24kbps モノラルで 8 分半まで 1.5MB に収まるが、**「5 分まで」のキリのいい値を UI に表示する** ほうがユーザー UX として明快で、画像やほかの会話と共存させたときの安全マージンも確保できる。

### 6.3. 将来拡張

これらの上限を超える需要が出てきたら、Files API 経由 (48 時間保持 URI) の経路を別 Intent Doc で設計する。短期では本書のインライン経路のみで運用する。

---

## 7. multimodal_input_pipeline.md との関係

| 観点 | multimodal_input_pipeline (ツール戻り値経路) | 本書 (ユーザー UI 添付経路) |
|---|---|---|
| メディアの起点 | ツール (MCP / ネイティブ) が `metadata.media[]` で返す bytes | ユーザーが UI で添付したファイル (HTTP POST) |
| 揮発性 | デフォルト ephemeral (pulse 単位で消える)、明示昇格で永続化 | 最初からファイル永続化 (ユーザーの添付は意図的なので残す) |
| handle 管理 | `MediaBuffer` が pulse 単位で handle_id 発行 | handle 不要、URI (`saiverse://audio/<filename>`) で直接参照 |
| Item 化 | LLM の `promote_media` ツール呼び出し経由 | UI 操作 (将来検討) or 既存 `pickup_item` 系経由 |
| 共通する出口 | `metadata.media[]` を経由して LLM client の `iter_*_media` → `load_*_bytes_for_llm` | 同左 |

両経路は **`metadata.media[]` の形式と `iter_*_media` / `load_*_bytes_for_llm` の実装を共有する**。設計を別々に進めても、共通インターフェースに合流するので衝突しない。実装順序として、本書 (ユーザー添付) を先に着手して media_utils の音声・動画拡張を済ませておけば、multimodal_input_pipeline の Phase 6 (Audio 対応) が乗りやすくなる。

---

## 8. 実装フェーズ

### Phase 1: 基盤 (ffmpeg + media_utils 拡張)

- `imageio-ffmpeg` 追加、`saiverse/ffmpeg_runner.py` 実装
- `media_utils.py` に音声・動画用の MIME / URI / iter / load / store / dir ヘルパ追加
- `media_summary.py` に `ensure_audio_summary` / `ensure_video_summary` 追加
- DB マイグレーション: `AI.AUDIO_MODEL`, `AI.VIDEO_MODEL` カラム追加

### Phase 2: API + ストレージ

- `api/routes/media.py` に `/upload-audio`, `/upload-video`, `/audio/{filename}`, `/video/{filename}` 追加
- `/upload-file` の auto 判定拡張
- 動画 90 秒チェック、サイズ上限、ffmpeg 失敗時 503

### Phase 3: LLM client (Gemini)

- `llm_clients/base.py` に `supports_audio` / `supports_video` フラグ追加
- `llm_clients/gemini.py` の `_convert_messages` で音声・動画 inline_data 構築
- `builtin_data/models/*.json` の Gemini 系モデルにフラグ追加

### Phase 4: 非対応モデルフォールバック

- 各 provider の content builder で `supports_audio`/`supports_video` が False の場合、media を skip + サマリテキスト注入
- 動作確認: Claude / OpenAI で音声添付すると「[音声: <要約>]」が text 末尾に入る

### Phase 5: frontend

- `FileUpload.tsx` の audio/video 分岐
- チャット入力欄からの添付 UI
- 添付メッセージバブルの audio/video 埋め込み再生

### Phase 6: ペルソナ設定 UI

- ペルソナ編集画面に `AUDIO_MODEL` / `VIDEO_MODEL` 選択欄を追加
- unset 時の挙動 (env → builtin default) をツールチップで説明

### Phase 7: アイテム連携 (Open 状態 + item_view)

- `Item.TYPE = "audio"` / `"video"` のアイテム作成経路 (frontend で添付メディアをアイテム化、または backend 側のアイテム生成ツール経由)
- `get_visual_context._render_item` の audio/video 分岐
- `ItemService.view_items_for_persona` の audio/video 対応 (戻り値スキーマの拡張方針は実装時に決定)
- Open/Closed 切替経路の確認、必要なら新規ツール追加
- saiverse://item/<id>/audio, saiverse://item/<id>/video の resolve 経路

### Phase 8: 統合テスト

- Gemini ペルソナで音声添付 → 内容理解できる
- Gemini ペルソナで 90 秒動画添付 → 内容理解できる
- Claude ペルソナで音声添付 → サマリテキストで内容把握
- 5 分超過音声・90 秒超過動画のアップロード拒否
- ffmpeg 未インストール環境での 503 応答
- Open 状態の audio アイテムを持たせて、新規添付音声の発話者判別を Gemini ペルソナで確認
- Closed のアイテムは description のみ参照されることを確認

各 Phase の完了基準と詳細スコープは実装着手時に Task 化する。

---

## 9. 未対応 / 将来拡張

- **動画の長尺対応 (Files API)**: 90 秒超の動画を扱いたくなったら、Files API 経由 + 48 時間キャッシュ管理を別 Intent Doc で設計
- **音声の長尺対応**: 現状は音声に長さ上限を設けない (opus 32kbps モノラルなら 60 分でも約 15MB)。9.5 時間まで Gemini 仕様上は可能だが、現実的には会話文脈で 10 分以上の音声を投げる用途が想定外
- **動画からの音声分離認識**: 動画は音声付き mp4 として送るが、「音声だけ取り出して別経路で認識」のニーズが出たら拡張
- **YouTube URL 直渡し**: Gemini が対応しているが、SAIVerse の URI スキームに `saiverse://youtube/<id>` のような追加が要る。需要次第
- **音声/動画の Item 化 UI**: 添付メッセージから「これを保存」操作で Item 化する UI。需要が出てから検討
- **既存ネイティブツール (image_generator 等) との統一**: multimodal_input_pipeline 側で議論されている共通基盤への移行は本書スコープ外

---

## 10. 関連ドキュメント

- [multimodal_input_pipeline.md](multimodal_input_pipeline.md) — ツール戻り値経路のメディア基盤 (本書と並走)
- [model_provider_management.md](model_provider_management.md) — モデル設定の 3 層優先 (AUDIO_MODEL / VIDEO_MODEL もこの体系に乗る)
- [Gemini API: Audio](https://ai.google.dev/gemini-api/docs/audio?hl=ja) — 音声入力の公式仕様
- [Gemini API: Video Understanding](https://ai.google.dev/gemini-api/docs/video-understanding?hl=ja) — 動画入力の公式仕様
- [imageio-ffmpeg (PyPI)](https://pypi.org/project/imageio-ffmpeg/) — ffmpeg バイナリ同梱パッケージ

---

## 改訂履歴

- v0.1 (2026-05-18): 起草。Gemini API の音声・動画入力をユーザー添付経由でペルソナに流す経路を設計。90 秒上限 + ffmpeg 正規化 + インライン送信で Files API を回避。multimodal_input_pipeline.md (ツール戻り値経路) と並走する独立経路として位置付け、media_utils.py の音声・動画拡張で出口を共有する。
- v0.2 (2026-05-18): まはーレビュー反映。(1) 音声 5 分上限・ビットレート 24kbps モノラル 16kHz に統一 (動画の音声トラックも同様)。「5 分」という UX 明快な値で会話履歴全体の 20MB 制約に余裕を持たせる。(2) C7 不変条件を追加: Open 状態の audio/video アイテムは picture/document と同じ思想で visual_context 経由で LLM 認知に乗る。(3) §4.8 アイテム閲覧経路の設計を追加: `get_visual_context._render_item` への audio/video 分岐、`item_view` / `ItemService.view_items_for_persona` の拡張方針。(4) §5 既存差分マップに `get_visual_context.py` / `item_view.py` / `manager/items.py` を追加。(5) §8 実装フェーズに Phase 7 (アイテム連携) を追加。
- v1.0 (2026-05-18): 実装完了。Phase 1-8 + ユーザー添付経路 (chat API の _store_audio/video_attachment、page.tsx の FileAttachment/getFileType 拡張) + グローバル設定モデルロールタブへの audio/video 追加 + 右サイドバー Open/Close 制御 + ItemModal で `<audio>` / `<video>` 再生 + チャットバブルで `<audio controls>` / `<video controls>` 再生まで全機能を実装。

  実装中に発見した追加修正:
  - `iter_image_media` バグ: `metadata["media"]` 内の audio/video エントリを type フィルタなしで image として拾っていた → type/mime_prefix フィルタを追加して分離
  - chat API history rendering バグ: `metadata.media[]` の audio/video が `images_list` に混入し、ユーザーバブルで「Attachment 1」と alt 表示される問題 → `_classify_and_append` で type 別に `images_buf` / `audios_buf` / `videos_buf` へ振り分け
  - `_AUDIO/VIDEO_SUMMARY_MODEL_RAW` のモジュール定数を `_get_*_summary_model_raw()` 関数に変更し、UI からモデル切替後にリスタート不要で反映されるよう改善 (画像も同様にリファクタ)
  - `manager/items.py:_resolve_file_path` と `api/routes/info.py:get_item_content` のパスリカバリに audio/video subdir 戦略を追加

  archive へ移動。今後の拡張 (item_view ツールの戻り値スキーマ拡張、ペルソナ単位 AUDIO/VIDEO_MODEL の動的読み込み、Files API 経由の長尺対応など) は §9 「未対応 / 将来拡張」にあるとおり、需要が出てから個別 Intent Doc を立てる。
