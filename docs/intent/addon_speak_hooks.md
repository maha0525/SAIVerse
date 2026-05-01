# Intent: アドオン向け発話完了 hook 機構

**ステータス**: ドラフト (まはーレビュー待ち)

## これは何か

ペルソナの発話 (`emit_speak` / `emit_say`) が Building history に記録されたタイミングで、宣言的に登録された **アドオンの Python 関数** をサーバー側で呼び出す汎用拡張点を本体に追加する。

アドオンは `addon.json` の `server_hooks` セクションで `event` と `handler` (`module:function` 形式) を宣言するだけで、SEA Playbook の構造に依存せずに発話イベントを購読できる。

最初の利用者は voice-tts アドオン (Track ベース化で動かなくなった TTS の復旧) だが、設計は voice-tts 専用ではない汎用機構として作る。

## なぜ必要か

### 問題: Track ベース化で sub_speak が共通経路でなくなった

2026-05-01 の認知モデル Phase C-2/3 で、対ユーザー会話と外部通信の発話経路が `track_user_conversation.json` / `track_external.json` に移行した。これらは `compose` (LLM) + `process_body` をインライン化し、旧 `sub_speak` Playbook を呼ばない構造になっている。

voice-tts アドオンは旧仕様の前提に立ち、`expansion_data/saiverse-voice-tts/playbooks/public/sub_speak.json` で本体の `sub_speak` を上書きし末尾に `tts_speak` ノードを追加するパターンで動いていた。Track Playbook はこれを通らないため、デフォルトのユーザー会話で TTS が完全に無音になっている。

### 問題: Playbook 上書きパターン自体が脆い

仮に Track Playbook を上書き対象に追加しても、本体側で Track 種別が増減するたびにアドオン作者が追従する必要がある。発話処理が複数経路に分散している以上、「Playbook の終端で TTS ノードを呼ぶ」という設計は本質的に維持コストが高い。

発話処理の最終共通経路は `sea/runtime_emitters.py` の `emit_speak` / `emit_say` であり、ここで一元的にイベント発火することで、Playbook 構造の変更にアドオンが影響を受けなくなる。

### 問題: サーバー側でアドオンコードを呼ぶ汎用 hook 機構が無い

既存のアドオン拡張点は以下の 3 種類:

| 拡張点 | 性質 | 呼び出し頻度 |
|---|---|---|
| `expansion_data/<addon>/api_routes.py` | 外部 HTTP リクエスト駆動 | 受動・低頻度 |
| `expansion_data/<addon>/integrations/*.py` | 独立 polling thread (`BaseIntegration`) | 周期実行 |
| `addon.json` の `oauth_flows.post_authorize_handler` | OAuth コールバック時のみ | 極低頻度 |

いずれも「本体が一定の宣言・規約を読んでアドオンの Python コードを import して呼ぶ」点では共通しており、新方式は前例の延長。ただし本機構は **本体内部イベント駆動・高頻度** という新しい性質を持つため、本体スレッドへの影響を遮断する仕組みが必要になる。

## 守るべき不変条件

### 1. 本体スレッドはアドオンハンドラに依存しない

ハンドラは `ThreadPoolExecutor` (max_workers=4) に投入し fire-and-forget。発火元の本体スレッドは即座に次へ進む。ハンドラがどれだけ重くても、どんな例外を出しても、発話処理本体の進行を止めない。

### 2. ハンドラ例外は隔離する

ハンドラが投げた例外は WARNING ログに記録し、他のハンドラへ伝播させない。1 つのアドオンの不具合が他のアドオンや本体を巻き込まない。

### 3. イベントは発話の意味的な「種類」ではなく「ペルソナが何か喋った」という単一抽象で表現する

`emit_speak` (旧式 speak ノード経由) と `emit_say` (LLM ストリーミング経由) は技術的には別経路だが、ユーザーから見れば両方とも UI のチャットバブルとして現れる発話。アドオン作者にとっても「ペルソナが何か喋った」という単一抽象で見える方が扱いやすい。

→ イベント名は単一の `persona_speak` とし、技術的な発火元の区別は payload の `source: "speak" | "say"` で表現する。これを必要とするアドオンは少数 (デバッグ用途程度) と想定。

### 4. テキストは生 / TTS 向けの両方を渡す

- TTS は `<in_heart>` 除去後・spell ブロック処理後の `text_for_voice` を必要とする (内心独白を読み上げない)
- 字幕・ログ・外部送信系のアドオンは生テキスト `text_raw` を必要とする可能性がある

ペイロードに両方含めることで、アドオン側で選択できる。本体側で 2 種類のテキストを構築するコストは小さい (`emit_say` は既に内部で除去処理済み、`emit_speak` は素通し)。

### 5. アドオン作者は SEA Playbook 構造を意識しなくてよい

「ペルソナが発話したら呼ばれる」というシンプルな契約のみ。Track Playbook が増えようが、Playbook が刷新されようが、本機構を経由する限り発火が保証される。

### 6. ペルソナ別有効化フィルタはアドオン側責務 (Phase 1)

「このペルソナでは TTS を鳴らさない」のような条件分岐はアドオン内で `get_params(addon_name, persona_id=...)` を呼んでチェックする。本体側で `requires_enabled_param` のような宣言型フィルタを提供することは Phase 1 ではしない (将来拡張の余地として残す)。

理由は (1) 今すぐ必要な仕組みではない、(2) アドオン側で書けば 1〜2 行で済む、(3) 宣言型にすると `client_actions` と同様の評価器が本体に必要になり Phase 1 のスコープを膨らませる、の 3 点。

### 7. 既存の `notify_unity_speak` は据え置き

Unity Gateway 通知も本来この hook 機構で表現できる (unity-gateway アドオン化) が、今回はスコープ外。`emit_speak` / `emit_say` 内で従来通り直接呼ぶ。将来 Unity Gateway をアドオン化する際にこの機構へ移行する。

## 設計

### A. `addon.json` の `server_hooks` セクション

```json
{
  "name": "saiverse-voice-tts",
  "version": "0.4.0",
  "params_schema": [...],
  "server_hooks": [
    {
      "event": "persona_speak",
      "handler": "speak_hook:on_persona_speak"
    }
  ]
}
```

**フィールド**:
- `event`: イベント名 (Phase 1 は `persona_speak` のみ)
- `handler`: `<module>:<function>` 形式。`expansion_data/<addon>/<module>.py` の `<function>` を指す。既存の `oauth_flows.post_authorize_handler` と同形式

複数ハンドラ宣言可 (配列)。複数アドオンが同一イベントを購読しても順序保証はしない (並列実行扱い)。

### B. イベントペイロード

```python
def on_persona_speak(
    persona_id: str,
    building_id: str,
    text_raw: str,           # <in_heart> 等タグ含む生テキスト
    text_for_voice: str,     # <in_heart> 除去 + spell ブロック処理後
    message_id: str,         # Building history の message_id
    pulse_id: str | None,
    source: str,             # "speak" or "say"
    metadata: dict,          # Building history メッセージの metadata 辞書
) -> None:
    ...
```

ハンドラ関数は **キーワード引数** で受ける規約。ペイロードに項目が増えても古いハンドラが壊れないよう、アドオン側は `**kwargs` で未知キーを吸収することを推奨。

### C. ディスパッチャ実装

新規モジュール: `saiverse/addon_hooks.py`

```python
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Any
import logging

LOGGER = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="addon-hook")
_handlers: Dict[str, List[Callable]] = {}

def register_speak_handler(event: str, handler: Callable) -> None:
    _handlers.setdefault(event, []).append(handler)

def dispatch_hook(event: str, **payload: Any) -> None:
    for handler in _handlers.get(event, []):
        _executor.submit(_safe_invoke, handler, payload)

def _safe_invoke(handler: Callable, payload: Dict[str, Any]) -> None:
    try:
        handler(**payload)
    except Exception:
        LOGGER.warning(
            "Addon hook failed: %s.%s",
            getattr(handler, "__module__", "?"),
            getattr(handler, "__name__", "?"),
            exc_info=True,
        )
```

### D. addon_loader への hook 登録機構追加

`addon_loader.py` (既存) に新メソッド `load_addon_server_hooks()` を追加:

```python
def load_addon_server_hooks() -> None:
    for addon_dir in EXPANSION_DATA_DIR.iterdir():
        if not is_addon_enabled(addon_dir.name):
            continue
        manifest = _load_addon_manifest(addon_dir)
        for hook_def in manifest.get("server_hooks", []):
            event = hook_def["event"]
            handler_spec = hook_def["handler"]
            module_name, _, fn_name = handler_spec.partition(":")
            module = _import_addon_module(addon_dir.name, addon_dir / f"{module_name}.py")
            handler = getattr(module, fn_name)
            register_speak_handler(event, handler)
```

`main.py` で `load_addon_server_hooks()` を起動時に呼ぶ (既存の `load_addon_integrations()` と同列)。

### E. `emit_speak` / `emit_say` の発火点

`sea/runtime_emitters.py` の各メソッド末尾 (Building history 記録と `set_active_message_id` の後) に dispatch を追加:

```python
# emit_speak の末尾 (現状の notify_unity_speak の前後)
if record_history and msg_id:
    from saiverse.addon_hooks import dispatch_hook
    from saiverse.content_tags import strip_in_heart
    dispatch_hook(
        "persona_speak",
        persona_id=persona.persona_id,
        building_id=building_id,
        text_raw=text,
        text_for_voice=strip_in_heart(text),
        message_id=str(msg_id),
        pulse_id=pulse_id,
        source="speak",
        metadata=dict(metadata),
    )
```

`emit_say` も同様 (`text_for_voice` には既に処理済みの `building_content` を渡せる)。

### F. アドオン側の hook ハンドラ規約

アドオン作者向けの最小例:

```python
# expansion_data/<addon>/speak_hook.py
def on_persona_speak(persona_id, text_for_voice, message_id, **kwargs):
    # ハンドラ内で重い処理は禁止 — 自前で Queue / Thread に投入すること
    do_something_lightweight(persona_id, text_for_voice, message_id)
```

ハンドラは ThreadPoolExecutor (max_workers=4) で実行されるため、長時間ブロッキング処理を書くとプール枯渇のリスクがある。**重い処理は自前で Queue / バックグラウンドスレッドに投入し、ハンドラ自体は即座に return する**ことを規約とする (voice-tts の `enqueue_tts()` パターン)。

### G. voice-tts アドオン側の移行作業

1. **新規追加**: `expansion_data/saiverse-voice-tts/speak_hook.py`
   ```python
   from tools._loaded.speak.playback_worker import enqueue_tts
   from tools._loaded.speak.text_cleaner import clean_text_for_tts
   from saiverse.addon_config import get_params

   def on_persona_speak(persona_id, text_for_voice, message_id, **kwargs):
       params = get_params("saiverse-voice-tts", persona_id=persona_id)
       if not params.get("auto_speak", True):
           return
       cleaned = clean_text_for_tts(text_for_voice)
       if not cleaned:
           return
       enqueue_tts(cleaned, persona_id, message_id)
   ```

2. **`addon.json` 変更**: `server_hooks` セクション追加、バージョンを 0.4.0 に bump

3. **削除**: `expansion_data/saiverse-voice-tts/playbooks/public/sub_speak.json`
   - 残すと旧 `meta_simple_speak` 経路で本体 hook と override の両方から TTS が呼ばれ二重発火する

## 設計判断の理由

### なぜ ThreadPoolExecutor で隔離するか

既存の `api_routes` / `integrations` / `post_authorize_handler` はいずれも本体スレッド外で動く (HTTP request worker、独立 polling thread、コールバック専用 endpoint)。一方 speak hook は `emit_speak` / `emit_say` 内部から呼ばれるため、何もしないと本体 (チャット応答ストリーミング) スレッドで実行されてしまう。

ハンドラ作者がうっかり同期 API を叩いたり Disk I/O を発生させたりした場合、本体のレスポンスが遅延する。これを防ぐため最初から ThreadPoolExecutor で隔離する。max_workers=4 は経験則 (アドオン数 × 同時発話数を考えても通常は 1〜2 で十分、burst を吸収できる程度に確保)。

### なぜイベント名を `persona_speak` 単一にするか

`emit_speak` / `emit_say` の差は本体実装の歴史的経緯 (旧式 speak ノード vs LLM ストリーミング) であり、アドオン作者にとっては内部実装の詳細。「Air が "こんにちは" と発話した」という事実は両者で同一であり、アドオンが扱うべき抽象も同一。

別イベントに分けると、新しい発話経路 (例: 将来追加される `emit_whisper` 等) が増えるたびにアドオン作者が購読対象を増やす必要が出る。単一イベント + payload の `source` フィールドでデバッグ用途には対応できる。

### なぜハンドラ宣言を `module:function` 形式にするか

既存の `oauth_flows.post_authorize_handler` と完全に同じ形式に揃える。アドオン作者が複数の hook 機構の規約をバラバラに覚える必要がなくなる。

### なぜ Phase 1 でペルソナ別宣言型フィルタを実装しないか

`requires_enabled_param: "auto_speak"` のような宣言型フィルタは便利だが:
- アドオン側で `get_params()` を呼べば 2 行で済む
- 宣言型にすると本体に評価器 (`client_actions` 風) が必要になる
- 「params の値が truthy ならフィルタ通過」というルールも、bool / dropdown / dict を統一的に評価する必要がある

Phase 1 のスコープを絞るためアドオン側責務とする。実装が増えてきて重複コードが目立ってきたら Phase 2 で宣言型を追加する。

### なぜ複数ハンドラの実行順序を保証しないか

順序保証すると ThreadPoolExecutor で並列実行できなくなり、隔離設計の利点が半減する。発話 hook は本質的に **副作用通知** であり、ハンドラ間に依存関係がある設計はそもそも避けるべき。順序が必要なシナリオ (例: ログを取った後に外部送信) は 1 つのハンドラ内で順次実行する。

## スコープ

### Phase 1 — 機構の追加 + voice-tts 移行

1. `saiverse/addon_hooks.py` 新規実装 (dispatcher + register API)
2. `addon_loader.py` に `load_addon_server_hooks()` 追加
3. `main.py` 起動時に `load_addon_server_hooks()` を呼ぶ
4. `sea/runtime_emitters.py` の `emit_speak` / `emit_say` に dispatch を追加
5. voice-tts アドオン側の移行 (`speak_hook.py` 新規 / `addon.json` 変更 / `sub_speak.json` 削除)
6. テスト: dispatcher の単体テスト (例外隔離、複数ハンドラ並列実行、ペイロード受け渡し)
7. 実機検証: Track 経由 / `meta_simple_speak` 経由の両方で TTS が鳴ること、auto_speak OFF のペルソナでスキップされること

### Phase 2 (将来、範囲外メモ)

- `requires_enabled_param` 等の宣言型フィルタ追加
- 他のイベント追加: `tool_call_finished`, `pulse_completed`, `persona_moved` 等
- Unity Gateway を unity-gateway アドオンとして切り出し、`notify_unity_speak` を本機構に移行
- ハンドラ実行のメトリクス収集 (どのハンドラが何 ms かかっているか)

## 検証観点

実機検証で必ず通すケース:
- Track Playbook 経由のユーザー会話で TTS が鳴る (現状の不具合の修正確認)
- `meta_simple_speak` 経由 (旧 sub_speak 経路) でも TTS が鳴る、二重発火しない
- スペル発火時の bubble1/bubble2 (`emit_say` 経路) でも TTS が鳴る
- voice-tts アドオンを無効化したら TTS が鳴らなくなる
- voice-tts アドオンを再有効化したら鳴る (ハンドラ register/unregister のライフサイクル)
- ペルソナの `auto_speak` パラメータ OFF で当該ペルソナの発話のみスキップされる
- ハンドラ内で意図的に例外を投げても本体の発話処理が継続する
- `<in_heart>` を含む発話で内心部分が読み上げられない (TTS 向けテキストの除去確認)

## 補足: 設計上の前提

- voice-tts は実質まはー一人だけが使っている状態のため、後方互換性のための移行レイヤー (旧 `sub_speak.json` を残しつつ新 hook も併用、等) は実装しない。アドオン側を一気に切り替える
- 本機構が他のアドオンで利用される際の設計レビューは、最初の利用ケース (字幕、ログ等) が出てきた時点で実施する
