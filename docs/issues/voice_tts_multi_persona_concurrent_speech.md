# Issue: 複数ペルソナ同時発話 × 共有 voice-tts の競合（後発ペルソナの音声が物理機体に届かない）

**ステータス**: 🔴 未解決（voice-tts 側のアーキテクチャ課題）
**優先度**: medium（複数機体の同時発話で顕在化。単一機体・単一発話では出ない）
**作成**: 2026-07-01（Stack-chan 複数機体テストで発見）
**関連**: `docs/issues/stackchan_multi_vessel_phase7_followups.md`、`docs/intent/stackchan_vessel.md`（TTS 出力経路 C-1）、memory `project_voice_tts_gil_starvation` / `project_voice_tts_remote_layout`

## 症状

複数の Stack-chan 機体にそれぞれ別ペルソナが降りて**同時期に発話**すると、**後から喋ろうとしたペルソナの TTS 音声が、その物理機体（スタックチャン）から出ない**。ブラウザ（client 側）では音声が再生される。首振り・LED・表情等の他の身体操作は正常。

## 根本原因

2 層の要因が重なる:

1. **voice-tts の TTS 生成が順次処理（serialize）**: voice-tts は GPU 1 個の共有エンジン（GPT-SoVITS 推論が GPU を占有）で、TTS 生成を 1 ペルソナずつ処理する。ペルソナ A が長い発話を生成している間、ペルソナ B の生成は開始されず待たされる。
2. **stackchan speak_hook の first-chunk timeout（60 秒）**: `expansion_data/saiverse-stackchan-addon/speak_hook.py` の `_wait_first_chunk`（`timeout_s=60.0`）が、`subscribe_pcm(message_id)` の最初の PCM チャンクを 60 秒待って来なければ abort する。A の発話が 60 秒を超えると、B の最初のチャンクが間に合わず speak_hook が中断 → B の機体に PCM が届かない。

B の音声は、A の生成完了後に voice-tts が生成して client 側で再生されるが、その時点で speak_hook は既に abort 済みなので物理機体には転送されない。

## 実機証拠（2026-07-01 セッション 20260701_211243）

- エア（1 号機 stackchan_room, vessel 076797f8）: `stackchan_room:694` の PCM POST が **634 秒・約 28 MB・音声 duration 444540 ms（≈7.4 分）** を送出し status=200 で完走。7 分程度の発話は正常（ユーザー確認済み）。
- アイフィ（2 号機 stackchan_2nd_room_city_a, vessel f0d40b0b）: 同時期（21:23）に speak_hook が subscribe（`already POSTing since sub_seq=1`）したが、`stackchan speak_hook: first chunk timeout (60.0s), abort`（21:24:21）→ PCM が 2 号機 gateway に届かず。
- 両ペルソナとも voice-tts の `server_side=False`（同一設定、差ではない）。

## 切り分け（これは何ではないか）

- **stackchan speak_hook のルーティングバグではない**: `_active_posts` は `vessel_id` キーで **機体ごとに独立した POST 枠**を持ち、A/B の POST は別々に立つ（単一枠の奪い合いではない）。
- **複数機体（Phase 7'）のルーティング／gateway／avatar の問題ではない**: それらは同セッションで正常動作を確認済み。本件は 1 層上の「TTS 生成の並行性」の問題。
- **server_side 設定の差ではない**: 両者 False で同一。

## 検討した対処

- **first-chunk timeout の延長／撤廃 → 不可**: voice-tts が順次処理する限り、B の音声は A の完了後（例では 7 分後）に生成される。timeout を延ばしても B の発話が数分遅れて機体から出るだけで、会話として破綻する。timeout はそもそも「本当に失敗したストリームを検出する」ためのもので、これを緩めるのは筋が悪い。

## 方向性の選択肢

- **(a) voice-tts を複数ペルソナ並行生成に対応**: 複数ワーカー / バッチ推論 / GPU 資源の割り当て等。根本解決だが重く、GPU 資源と voice-tts（別リポジトリ、origin=Nature109）の設計に踏み込む。
- **(b) チャンク単位のインターリーブ**: A の生成中でも B のチャンクを合間に生成して両ストリームを進める。voice-tts のスケジューラ改修が要る。
- **(c) 当面は制約として受容**: 「複数機体の同時発話は片方が待つ（長い発話中は他方の機体音声が出ないことがある）」を既知の制約として明記し、Phase 7'（複数機体）は "発話の並行性以外は完成" として区切る。voice-tts の並行化は独立テーマとして別途扱う。

現時点の暫定方針: **(c)**。Stack-chan 複数機体対応の本丸（起動・ルーティング・capability・avatar・複合アクション）は完成。voice-tts の複数ペルソナ並行生成は別軸の課題として本 issue で追跡する。

## 関連コード

- `expansion_data/saiverse-stackchan-addon/speak_hook.py` — `_wait_first_chunk`（60s timeout）、`_active_posts`（per-vessel POST 枠）、`on_persona_speak`
- voice-tts（別リポジトリ）— `subscribe_pcm` / PCM broadcast / TTS 生成ワーカー
