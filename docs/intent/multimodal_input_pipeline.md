# Intent: マルチモーダル入力経路（画像 / 音声 / 動画 / 汎用binary）

**ステータス**: ドラフト v0.1（2026-05-01 起草）

## これは何か

ペルソナの LLM 呼び出しに、**ツール戻り値由来のマルチモーダル content（画像・音声・動画・汎用binary）を流すための汎用基盤**。MCP ツールとネイティブツールのどちらから返ってきても同じ経路で扱う。アドオン作者が「これは attachment として LLM に見せるだけ」「これはアイテム化して世界に残す」を**コンテンツ単位で選べる**インターフェースを提供する。

着手契機は WiFi Camera MCP アドオン（TP-Link Tapo カメラを操作）の採用判断。「ペルソナが部屋を眺める」体験には、MCP `ImageContent` がペルソナの LLM 入力に届く必要があるが、現状の `tools/mcp_client.py::_format_tool_result` (~229-249行) は binary を `[binary: N bytes]` 文字列に潰してしまう。これを直すついでに、汎用基盤として整備する。

## これは何でないか

- **MCP プロトコル全機能の対応計画ではない**。本書は ImageContent / AudioContent / EmbeddedResource の入力側経路に絞る。Resources / Sampling / Elicitation 等は `docs/intent/mcp_protocol_coverage.md` を参照。
- **新しい Item システムの設計ではない**。既存の `Item` テーブルと `ItemLocation` (`database/models.py:201-226`) をそのまま使う。
- **特定のメディア種別への特化ではない**。最初は image と audio を主対象とするが、video / binary も同じ型で扱える設計にする（個別特化型を避けるのは SAIVerse 全体の設計哲学）。
- **既存ツール（`image_generator.py` 等）の置換ではない**。既存ツールは現状維持。新ツール（特に MCP 経由）が新基盤を使えればよい。

## なぜ必要か

### 問題1: マルチモーダル content の流通経路が断絶している

ツール戻り値が文字列のみで、binary content（画像 bytes、音声 bytes 等）を LLM 入力までキャリーする経路がない。`LLMClientBase` 側には `supports_images` と `_store_attachment / consume_attachments` (`llm_clients/base.py:36-130`) で attachment 流通の基盤があり、Anthropic / OpenAI / Gemini の各 client が attachment を base64 化して LLM に送る実装も既にある。しかし、**ツール戻り値からこの attachment 経路に乗せる中間レイヤが、ネイティブツール（`image_generator.py`）の個別実装としてしか存在しない**。MCP ツール側からはこの経路に何も流せない。

### 問題2: 揮発と永続の選択ができない

「カメラで部屋を確認する」ユースケースを考えると、5分おきに撮影した画像をすべてアイテム化するのは現実的でない（1日で数百アイテム発生）。一方で、撮影画像が「侵入者発見」のような重要な瞬間を捉えていたら、ペルソナがそれを世界に残せる必要がある。

つまり、**ツールが返した content をどう扱うかは、ツール側 / ペルソナ判断 / アドオン側で動的に選べないといけない**。SAIVerse 本体が「常にアイテム化」「常に揮発」を強制する設計は事故になる。

### 問題3: メディア種別の汎用性が将来必要になる

画像専用 attachment スキーマは今すぐ動くが、近い将来に音声入力（カメラ MCP の次の段階、または常時音声入力アドオン）が来る。設計時点で audio / video / 汎用 binary を想定した型にしておかないと、後から音声追加時に attachment スキーマの破壊的変更が必要になる。

## 守るべき不変条件

### 1. SAIVerse 本体は「選択肢を提供する基盤」に徹する

「常にアイテム化」「常に揮発」のどちらかを本体が強制してはならない。判断はアドオン側 / ツール側 / LLM の動的判断に委ねる。本体の責任は「3パターンを簡単に選べるインターフェースを提供する」こと。

### 2. ツール戻り値からアイテム化までを SAIVerse 本体が自動でつないではならない

現状の `image_generator.py` は **ツール側が明示的に** `manager.create_picture_item()` を呼んでアイテム化している。新基盤でも、SEA runtime 層で「マルチモーダル content が来たら自動でアイテム化」する横断処理は入れない。ツール側の `disposition` 宣言、または LLM の `promote_media` 呼び出しという**明示的選択**を介してのみアイテム化が起こる。

### 3. ペルソナは「どのインスタンスを使うか」を意識しない（既存 MCP 不変条件の継承）

shared scope の MCP サーバー（カメラ等）でも、attachment / handle_id は **呼び出しペルソナの context に帰属**する。ペルソナ間で handle が漏れたり混ざったりしてはならない。

### 4. 揮発バッファの寿命は pulse 単位

`MediaBuffer` は pulse_id 単位で管理し、pulse 終了時に自動破棄する。promote されなかった handle は確実に消える。これによって「LLM が忘れた = 履歴にも残らない」という認知モデルの一貫性を保つ。

### 5. handle_id を介したパイプラインを許す

LLM が同一 pulse 内で `image_analyze(handle_id=...)`, `image_compare(handle_a, handle_b)` のように handle を再利用してツールを連鎖呼び出しできる。これによって「カメラ撮影→OCR→翻訳」のような複合処理を playbook で組める。

### 6. 既存ネイティブツールの動作を壊さない

`image_generator.py` 等の既存ツールは現状の経路を維持する。新基盤に乗せ換えるかは将来検討。後方互換性の担保。

## 設計

### A. コア型: MediaContent（ツール戻り値）

ツールは戻り値の metadata dict 内 `media` リストに以下を含めて返す:

```python
{
    "kind": "image" | "audio" | "video" | "binary",
    "data": bytes,           # data か path のどちらか必須
    "path": str,             # 既にファイルに保存済みならパス、なければ data を渡す
    "mime_type": str,
    "disposition": "ephemeral" | "file" | "item",
    "item_hint": {           # disposition="item" 時に使用
        "name": str,
        "description": str,
        "location": "world" | "building" | "inventory",  # デフォルト "building"
    },
    "alt_text": str,         # Vision/Audio 非対応モデル向けフォールバック説明
}
```

### B. disposition 3値の挙動

| disposition | LLM 現ターン視認 | 履歴で見える | inventory | promote_media で昇格可 |
|---|---|---|---|---|
| `ephemeral` | ○（attachment 経由） | ✕（handle と共に消える） | ✕ | ○ → file または item |
| `file` | ○ | ○（path metadata 残る） | ✕ | ○ → item のみ |
| `item` | ○ | ○（item_id 経由、`item_view` 可） | ○ | ✕（既に最終形態） |

各 disposition の意図:
- **`ephemeral`**: LLM が現ターンで判断材料として使うが、残す価値がなければ忘れて構わない content。デフォルト。
- **`file`**: 履歴で再表示できるよう保存はしておくが、ペルソナの inventory には入れない。「部屋ログ」のような位置付け。
- **`item`**: 世界に残す。inventory / building / world のいずれかに配置。

### C. MediaBuffer（新規コンポーネント）

`saiverse/media_buffer.py` で実装:

```python
class MediaBuffer:
    """pulse_id 単位の揮発メディアバッファ"""

    _buffers: Dict[str, Dict[str, MediaDescriptor]]  # pulse_id → handle_id → descriptor

    def register(pulse_id: str, kind: str, data: bytes,
                 mime_type: str, alt_text: str = "") -> str:
        """揮発 content を登録し handle_id を返す"""

    def get(pulse_id: str, handle_id: str) -> MediaDescriptor:
        """handle 参照（同一 pulse 内のツール連鎖から呼ばれる）"""

    def promote_to_file(pulse_id: str, handle_id: str) -> Path:
        """ファイル保存。kind に応じて適切なディレクトリへ"""

    def promote_to_item(pulse_id: str, handle_id: str,
                        name: str, description: str,
                        owner_kind: str, owner_id: str) -> str:
        """ファイル保存 + Item レコード作成"""

    def cleanup(pulse_id: str) -> None:
        """pulse 終了時に呼ばれる、未昇格 handle を破棄"""
```

#### handle_id 命名規則

`{kind}_{6桁hex}` 形式。例: `image_a3f01c`, `audio_b7k299`, `video_ce4521`。kind が見える形式にすることで、LLM が handle を見ただけでメディア種別を理解できる。

#### 容量上限

1 pulse 内で MediaBuffer に登録できる handle 数のデフォルト上限は **20**。環境変数 `SAIVERSE_MEDIA_BUFFER_LIMIT` で変更可能。上限超過時は最古の handle を破棄（FIFO）し、警告ログを出す。DoS 防止と、現実的な認知負荷の上限として設定。

#### 保存先ディレクトリ

| kind | 保存先 |
|---|---|
| image | `~/.saiverse/image/` (既存) |
| audio | `~/.saiverse/audio/` (新設) |
| video | `~/.saiverse/media/video/` (新設) |
| binary | `~/.saiverse/media/binary/` (新設) |

### D. promote_media ツール（新規 builtin）

`builtin_data/tools/promote_media.py` で実装:

```python
def promote_media(
    handle_id: str,
    to: Literal["file", "item"],
    name: Optional[str] = None,        # to="item" 時に必須
    description: Optional[str] = None, # to="item" 時に必須
    location: Literal["world", "building", "inventory"] = "building",
) -> str:
    """揮発メディアをファイルまたはアイテムに昇格させる"""
```

呼び出し側の責務:
- handle_id は同一 pulse 内のもののみ使用可（他 pulse の handle は `ToolError`）
- `to="item"` の場合、`name` と `description` は必須
- `location` は `to="item"` 時のみ意味を持つ

`location` の DB マッピング（既存 `ItemLocation.OWNER_KIND` 4値のうち3つを使用）:

| location 引数 | OWNER_KIND | OWNER_ID |
|---|---|---|
| `"world"` | `"world"` | 呼び出しペルソナの city_id |
| `"building"` (デフォルト) | `"building"` | 呼び出しペルソナの現在 building_id |
| `"inventory"` | `"persona"` | 呼び出しペルソナ ID |

`bag` (rucksack 入れ子) は本基盤のスコープ外。将来検討。

#### デフォルト location が building である理由

カメラで撮った写真が「その部屋に残る」が認知モデルとして自然。inventory は能動的に持つ感覚（積極的選択）、world は city 全体に放出する広範囲操作で、いずれもデフォルトとして強すぎる。LLM が「これは持っておきたい」「これは皆に見せたい」と判断したら明示的に指定する。

#### 履歴メッセージの metadata 書き換え

handle が promote されたら、その handle が登録されたメッセージ（ツール戻り値メッセージ）の metadata を、`ephemeral` descriptor から `file` または `item` descriptor に書き換える。これにより、過去メッセージを履歴で見返したときに、昇格された画像/音声が引き続き見える。

### E. MCP 戻り値処理の拡張

`tools/mcp_client.py::_format_tool_result` を以下のように改修:

1. `result.content` を走査
2. `TextContent` はそのまま rendered text に追加
3. `ImageContent` / `AudioContent` を検出したら:
   - 現在の pulse_id を `tools.context` から取得
   - `MediaBuffer.register()` に bytes を登録、handle_id を取得
   - rendered text に `[media handle=<handle_id> kind=<kind> mime=<mime>]` 注釈を追加
4. `EmbeddedResource` で `mimeType` が image/audio に該当する場合も同様
5. 戻り値型を変更: `str` → `Tuple[str, List[MediaDescriptor]]`（または metadata dict 経由）

呼び出し元（`call_tool` の戻り経路）でも、戻り値型変更に追従する。

#### MCP アドオンの disposition 宣言

`mcp_servers.json` の `spell_tools[]` に `media_disposition_default` フィールドを追加:

```json
"spell_tools": [
  {
    "name": "camera_capture",
    "display_name": "部屋を見る",
    "visible": true,
    "media_disposition_default": "ephemeral"
  }
]
```

未指定時は `"ephemeral"` がデフォルト（事故防止）。`addon.json` の `params_schema` でも persona 単位で上書き可能にすることで、UI からの ON/OFF 切替の素地を作る（実装は需要が出てから）。

### F. SEA runtime の拡張

`sea/runtime.py::_append_tool_result_message` を以下のように拡張:

1. ツール戻り値のうち、media descriptor リストを抽出
2. tool result message の `metadata` に `{"media": [descriptor, ...]}` を格納
3. 既存の attachment 経路（`iter_image_media` 等）が metadata から media を抽出する流れに合流

`store_structured_result` 周辺にも media descriptor を扱う経路を追加。

### G. LLM client 側の拡張

#### 1. supports_images の拡張

`LLMClientBase.__init__` の `supports_images` に加えて、`supports_audio`, `supports_video` フラグを追加:

```python
def __init__(
    self,
    supports_images: bool = False,
    supports_audio: bool = False,
    supports_video: bool = False,
):
```

各 provider client の `__init__` でモデル固有のサポート状況を宣言（モデル config ファイルから読む）。

#### 2. provider 別の audio block 構築

- **Anthropic**: `input_audio` ブロック（base64 + format）
- **OpenAI**: `gpt-4o-audio-preview` 系で `input_audio` パート
- **Gemini**: `inline_data` で `mimeType: "audio/wav"` 等
- **その他**: `supports_audio=False` で attachment 経路スキップ

各 provider 用に `_collect_attachment_state` / `_build_*_content_blocks` を audio 対応に拡張。

#### 3. 新 MIME type 列挙

`saiverse/media_utils.py:22` の `SUPPORTED_LLM_IMAGE_MIME` に加えて、`SUPPORTED_LLM_AUDIO_MIME` (`audio/wav`, `audio/mp3`, `audio/ogg` 等) を新設。

### H. Vision/Audio 非対応モデル時のフォールバック

各 disposition での挙動:

- **attachment 経路**: 該当メディアを乗せない（既存 `supports_images=False` 時の挙動を踏襲）
- **text への注釈**: `[画像が届きました（このモデルでは表示できません）handle=image_a3f]` のように handle と非対応である旨を明示
- **alt_text の併記**: ツールが `alt_text` を返していれば `[画像: handle=image_a3f, 説明: <alt_text>]` の形で text に含める
- **promote_media は引き続き呼べる**: メタ情報（kind, mime_type, alt_text）だけで「ファイル化はしておこう」とペルソナが判断できる

### I. ライフサイクル全体図

```
[pulse 開始: pulse_id 発行]
  ↓
ツール呼び出し (例: camera_capture)
  ↓
ツール内: 画像 bytes を返却 (disposition="ephemeral")
  ↓
SEA runtime: media descriptor を MediaBuffer に登録、handle_id 発行
  ↓
SEA runtime: tool result message の metadata に media descriptor 追加
  ↓
次の LLM 呼び出し: attachment 経路で実画像を LLM に表示
  + text に [media handle=image_a3f kind=image mime=image/png] 注釈
  ↓
LLM 判断:
  (A) 「異常なし」→ 何もせず終了
  (B) 「侵入者発見」→ promote_media(handle="image_a3f", to="item",
                                     name="侵入者", description="...",
                                     location="building")
  ↓
[pulse 終了]
  ↓
MediaBuffer.cleanup(pulse_id):
  (A) のケース: handle_id 廃棄、履歴にはテキスト記録のみ残る
  (B) のケース: handle は既に Item として永続化済み、buffer から除去
```

## 既存コードへの変更マップ

| ファイル | 変更内容 |
|---|---|
| `saiverse/media_buffer.py` | **新規**: MediaBuffer クラス、handle 管理、promote メソッド |
| `builtin_data/tools/promote_media.py` | **新規**: promote_media ツール |
| `tools/mcp_client.py:229-249` | `_format_tool_result` を ImageContent/AudioContent 対応に拡張、戻り値型変更 |
| `tools/mcp_client.py:355-383` | `call_tool` の戻り値型変更追従 |
| `tools/mcp_config.py` | `spell_tools[].media_disposition_default` の読み込み追加 |
| `sea/runtime.py:1331-1351` | `_append_tool_result_message` で media descriptor を metadata に格納 |
| `sea/runtime.py` | pulse 開始/終了時に MediaBuffer の register/cleanup フック追加 |
| `llm_clients/base.py` | `supports_audio`, `supports_video` フラグ追加 |
| `llm_clients/anthropic_request_builder.py` | audio block 構築追加 |
| `llm_clients/openai_message_preparer.py` | audio block 構築追加 |
| `llm_clients/gemini.py` | audio inline_data 対応追加 |
| `saiverse/media_utils.py` | `SUPPORTED_LLM_AUDIO_MIME` 追加、`store_audio_bytes` 等の追加 |
| `tools/context.py` | `get_active_pulse_id()` ヘルパ追加（MediaBuffer から参照） |

## アドオン作者向けの API（要約）

### MCP アドオンの場合

`mcp_servers.json` で `media_disposition_default` を宣言するだけ:

```json
"spell_tools": [
  {"name": "camera_capture", "media_disposition_default": "ephemeral"}
]
```

戻り値の `ImageContent` / `AudioContent` は SAIVerse が自動で MediaBuffer に登録し、handle_id を text に注釈してくれる。アドオン側で何も書かなくていい。

### ネイティブツールの場合

戻り値 metadata の `media` リストに MediaContent dict を含めて返す:

```python
def my_tool(...):
    return (text_summary, ToolResult(...), file_path_or_None, {
        "media": [{
            "kind": "image",
            "data": image_bytes,
            "mime_type": "image/png",
            "disposition": "ephemeral",
            "alt_text": "玄関のカメラ画像",
        }]
    })
```

## 未対応 / 将来拡張

- **TTL 付き pulse 跨ぎバッファ**: 現状は pulse 終了で確実に破棄。将来「短期記憶として N 分保持」を追加する可能性あり。実装は需要が出てから。
- **アドオン管理 UI からの disposition ON/OFF 切替**: addon.json の params_schema に `media_disposition_override` を加える素地は作るが、UI 実装は別タスク。
- **bag location**: `OWNER_KIND="bag"` (rucksack 等の入れ子) への配置は本基盤スコープ外。
- **ストリーミング応答**: カメラのリアルタイム映像、長時間録音などの ストリーミング content は別 Intent Doc（MCP Progress notifications との統合）。
- **既存ネイティブツールの新基盤への移行**: `image_generator.py` 等を新基盤に揃え直すのは将来検討。

## 関連ドキュメント

- `docs/intent/mcp_protocol_coverage.md` — MCP プロトコル機能の対応範囲。本書はその ImageContent / AudioContent 入力経路の具体化
- `docs/intent/mcp_addon_integration.md` — MCP × Addon 統合（実装済み）。本書は同基盤上に乗る
- `docs/features/mcp-integration.md` — 現状の MCP 機能ドキュメント
- `docs/intent/x_integration.md` — 投稿系の画像取り扱い（出力側）

## 実装フェーズ案

1. **Phase 1 - コア基盤**: MediaBuffer, promote_media ツール, MediaContent 型定義
2. **Phase 2 - MCP 接続**: `_format_tool_result` 拡張、`mcp_servers.json` の `media_disposition_default` 対応
3. **Phase 3 - SEA runtime 統合**: `_append_tool_result_message` 拡張、pulse ライフサイクルとの接続
4. **Phase 4 - LLM client 拡張（画像のみ先行）**: 既存 image attachment 経路と新 MediaBuffer 経路の接続
5. **Phase 5 - WiFi Camera MCP アドオン段階1**: 接続検証、画像が見える状態を実機確認
6. **Phase 6 - Audio 対応**: provider 別 audio block 構築、SUPPORTED_LLM_AUDIO_MIME 追加
7. **Phase 7 - Video / Binary**: 同上の拡張、需要に応じて

各 Phase の完了基準と詳細スコープは実装着手時に Task 化する。
