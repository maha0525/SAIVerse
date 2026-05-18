# Issue: stackchan-mcp upstream への PR 投稿戦略 (Phase X')

**ステータス**: 🔲 未着手 (= Phase X' 着手前のハンドオフ)
**優先度**: medium
**作成日**: 2026-05-13
**最終更新**: 2026-05-15 (Series E = 動的 avatar セット転送 PR を追補節として追加)
**関連**: `docs/intent/stackchan_vessel.md` §「Phase X'」、`docs/intent/stackchan_avatar_pipeline.md` §E、`maha0525/stackchan-mcp` fork branches `feature/external-pcm-stream` / `feature/dynamic-avatar-set`

## 背景

Phase 1' 完了時点で、fork (= `maha0525/stackchan-mcp`) の `feature/external-pcm-stream` ブランチに **9 commit が直線的に重なった状態**になっている。Phase X' で upstream (`kisaragi-mochi/stackchan-mcp`) に PR 投稿するには、これを **論理単位で分割した個別 PR (= Stacked PR 含む)** に整理する必要がある。1 つの巨大 PR にすると reviewer 負担大、merge も all-or-nothing で動かなくなる。

このドキュメントは「どの commit をどの PR にまとめ、どんな base 関係で出すか」「ブランチ分割の具体手順」を残す。

## 現在の commit 配列 (= `feature/external-pcm-stream` HEAD → root 方向)

| # | commit | 内容 | 論理 PR | 依存 |
|---|---|---|---|---|
| 9 | `5e1042a` | `client_max_size=0` で aiohttp streaming body cap 撤去 | **PR7** | PR3 (= `POST /pcm` を導入してる) |
| 8 | `93435a6` | persistent WS connection — NVS flag `websocket.persistent` opt-in (起動時 OpenAudioChannel + 失敗時 ScheduleReconnect) | **PR6b** | PR6a (= `intentional_close_` bug fix) |
| 7 | `f8927b8` | `intentional_close_` flag を OpenAudioChannelInternal 失敗パスでクリア | **PR6a** | 独立 (pure bug fix) |
| 6 | `e23fa57` | `_libs/` を `os.environ["PATH"]` にも prepend (= `ctypes.util.find_library` 経路救済) | **PR5b** | PR5a (= 同梱した DLL を見せる仕上げ) |
| 5 | `c7536f2` | Windows 用 `_libs/opus.dll` 同梱 + `os.add_dll_directory()` + hatchling `force-include` | **PR5a** | 独立 |
| 4 | `71dc11e` | xiaozhi-cloud OTA `CheckVersion()` の呼び出し撤去 (= NVS websocket.url が上書きされない) | **PR4** | 独立 |
| 3 | `b841d32` | HTTP `POST /pcm` endpoint を `capture_server.py` に追加 | **PR3** | PR2 (= `send_pcm_stream` を内部呼び出し) |
| 2 | `e5a83fa` | `send_pcm_stream(gateway, async iterator)` を追加 | **PR2** | PR1 (= `send_pcm_audio` を抽出してから差分が小さくなる) |
| 1 | `5df0460` | `send_pcm_audio(gateway, pcm, *, source_rate, source_label)` を `synthesize_and_send` から抽出 | **PR1** | 独立 |

CI/build 系の commit (`b0779e6` = `ci: trigger build on feature/** branches and via workflow_dispatch`) は fork-only の dev infrastructure 改善で、upstream PR には含めない。

## PR 依存グラフ (= upstream に投げる順)

```
PR1 (send_pcm_audio 抽出)
  └─ PR2 (send_pcm_stream)
      └─ PR3 (POST /pcm endpoint)
          └─ PR7 (client_max_size=0)

PR4 (OTA skip)              ── 独立

PR5a + PR5b → PR5 (libopus 同梱 + DLL search path) ── 独立、5a/5b は密接なので 1 PR に合体

PR6a → PR6 (persistent connection + bug fix) ── 6a/6b は密接なので 1 PR に合体
```

つまり最終的な PR 数:

| PR | 内容 | base | 依存 |
|---|---|---|---|
| **PR #A1** | refactor(tts): extract `send_pcm_audio` from `synthesize_and_send` | upstream `main` | — |
| **PR #A2** | feat(tts): `send_pcm_stream` for incremental PCM push | PR #A1 | PR #A1 が merge されてから提出 |
| **PR #A3** | feat(capture_server): `POST /pcm` endpoint for external PCM input | PR #A2 | PR #A2 が merge されてから提出 |
| **PR #A4** | fix(capture_server): disable aiohttp `client_max_size` cap | PR #A3 | PR #A3 が merge されてから提出 |
| **PR #B** | fix(firmware): skip xiaozhi-cloud OTA check (NVS websocket.url 保護) | upstream `main` | — |
| **PR #C** | fix(gateway): bundle libopus.dll for Windows + DLL search path setup | upstream `main` | — |
| **PR #D** | feat(firmware): opt-in persistent WS connection + reconnect bug fix | upstream `main` | — |

= **計 7 PR** (Phase 1' 由来、音声経路 + OTA + libopus + persistent WS)。Series A の 4 PR は順次積み上げ、B/C/D は独立並列で出せる。

これに加え、Phase 4.5-e で **Series E (PR-E1 / PR-E2)** が動的 avatar セット転送機構として投稿予定。本書末尾「追補: Series E」節を参照。

## ブランチ分割手順

依存関係的に PR #A1 → A2 → A3 → A4 は **Stacked PR** にする必要がある (= 前段が merge される前に後段を出すならその branch を前段 PR の branch から派生)。upstream maintainer の好み次第で「全部 main 直結 PR + 順次 merge 待ち」「Stacked PR で先行 review」のどちらかを選ぶ。

具体的なブランチ作成 (= 各 PR ごとに新 branch を fork に作る):

```bash
cd temp/stackchan-mcp

# upstream main を最新化
git fetch upstream
git switch -c pr-a1-extract-send-pcm-audio upstream/main
git cherry-pick 5df0460
git push origin pr-a1-extract-send-pcm-audio
# → fork から upstream/main へ PR を出す (= PR #A1)

# PR #A1 base で A2 を積む (Stacked):
git switch -c pr-a2-send-pcm-stream pr-a1-extract-send-pcm-audio
git cherry-pick e5a83fa
git push origin pr-a2-send-pcm-stream
# → PR description で「Stacked on #A1」を明示

# PR #A3:
git switch -c pr-a3-pcm-endpoint pr-a2-send-pcm-stream
git cherry-pick b841d32
git push origin pr-a3-pcm-endpoint

# PR #A4:
git switch -c pr-a4-client-max-size-cap pr-a3-pcm-endpoint
git cherry-pick 5e1042a
git push origin pr-a4-client-max-size-cap

# PR #B (独立):
git switch -c pr-b-skip-xiaozhi-ota upstream/main
git cherry-pick 71dc11e
git push origin pr-b-skip-xiaozhi-ota

# PR #C (PR5a + PR5b 合体):
git switch -c pr-c-bundle-libopus-windows upstream/main
git cherry-pick c7536f2
git cherry-pick e23fa57
git push origin pr-c-bundle-libopus-windows
# (or git rebase -i で 1 commit に squash してもよい — レビューしやすさ次第)

# PR #D (PR6a + PR6b 合体):
git switch -c pr-d-persistent-ws-opt-in upstream/main
git cherry-pick f8927b8
git cherry-pick 93435a6
git push origin pr-d-persistent-ws-opt-in
```

`feature/external-pcm-stream` 本体は手元検証用 (= addon の `mcp_servers.json` がここを指してる) に残す。upstream merge が進んだ段階で addon の `mcp_servers.json` を PyPI 公開版 (`uvx stackchan-mcp[tts]`) に切り替える。

## PR ごとの注意点

### PR #A1〜A4 (Series A — TTS / PCM 入力経路)

- A1 は純粋 refactor、テストが既存のままパスするはず → review しやすい
- A2 で `send_pcm_stream` を追加するときは新規 test を追加すると merge 確率上がる (= 既存 send_pcm_audio test と並列の async test)
- A3 の `POST /pcm` endpoint は test 必須 (= aiohttp test client で 200/401/503 を網羅)
- A4 の `client_max_size=0` は test しづらいが、PR description で「200-second long PCM upload で 1 MiB cap が阻む observed evidence」を Phase 1' のログから引用して根拠化

### PR #B (xiaozhi OTA skip)

- upstream maintainer (kisaragi-mochi) は xiaozhi-esp32 fork を強く意識した repo を持っているので、「xiaozhi-cloud と関係を断つ修正」は理由を厚めに書く必要あり
- PR description で「stackchan-mcp は自前 gateway を持つ前提で、xiaozhi-cloud OTA endpoint への接続は NVS websocket.url を server 主導で書き換える副作用がある」「自前 gateway 設計と矛盾する」を明示
- 受け入れ拒否される可能性が他の PR より高い (= xiaozhi 連携を残したい reviewer の意向次第)。拒否されたら手元 fork で運用継続が許容範囲 (= memory `feedback_user_experience_first.md`)

### PR #C (libopus bundle)

- バイナリを git に commit する点が受け入れ可否の分かれ目
- `_libs/SOURCES.md` で PyOgg 由来であることを明記してるが、レビュー時に「公式 xiph/opus source からの CI ビルドに置き換えたい」と要求される可能性大
- 受け入れ条件として CI に「windows-latest runner で vcpkg build → wheel に同梱」の workflow 追加を併せて提案するのが筋。これがあれば「git commit するバイナリは provisional、CI が正規」と説明できる

### PR #D (persistent WS connection)

- NVS flag opt-in なので default 挙動は変わらない → upstream の voice-session-driven なユーザーには影響ゼロ
- PR description で「stackchan-mcp の HTTP POST /pcm endpoint は既に外部 producer 受け入れの設計があるが、device が常時接続じゃないと届かない (= 503 を返す)」「server-driven push 用途で needed」を明示
- `intentional_close_` bug fix 部分 (= PR6a 相当の修正) は単体でも merge 価値あるので、persistent connection の opt-in 機能とは別 commit のまま組まれる (= reviewer は bug fix 単独で merge 可能)

## 投稿タイミング

Phase X' の前提条件:

- Phase 1' の動作実機検証完了 ✅ (= 2026-05-13 達成)
- Phase 2'-4' の実機検証完了 (= ペアリング UX、STT、ネイティブツール群)
- Phase 5' の実機検証 (= タッチ知覚)
- Phase 6' の実機検証 (= Avatar 連動)

これら全部終わってから PR 整形 + 投稿に着手。理由: 上流に投げた後で「実は別の修正が必要」と分かると PR 修正が大変、かつ「実機で本当に有用だった」を示せた状態で PR を出す方が受け入れ確率高い。

PR ごとに maintainer とディスカッションが入る前提で、各 PR review-cycle に 1-2 週間想定。全 PR merge には 1-3 ヶ月かかる可能性がある。

## 残課題 (Phase X' 実施時に解決すべき)

- **PR #C のバイナリ調達経路**: 現状の PyOgg 由来は暫定。CI ビルドへの切替 (= vcpkg + windows-latest workflow) を実装する PR を併走で提出する形が望ましい
- **Stacked PR の運用**: GitHub の Stacked PR ツール (= `Graphite` 等) を使うか、PR description で base を明示するだけかは upstream maintainer の好み次第
- **テスト追加の範囲**: A2/A3 で test を増やすコミットは別 commit にして PR に追加する (= 既存 commit のままだと test が無いのが目立つ)
- **`SOURCES.md` の更新**: PR #C 提出前に「CI build に置き換え予定」を補強する記述を入れておくと merge しやすい

## 追補: Series E — 動的 avatar セット転送機構 (Phase 4.5-e、2026-05-15 追記)

Phase 4.5 で新規に立てた intent doc (`docs/intent/stackchan_avatar_pipeline.md`) の作業ブランチ `feature/dynamic-avatar-set` から、upstream に **2 PR** を投稿する。Series A〜D とは別系統 (= avatar 描画基盤の拡張) で、依存関係も独立。

### PR-E1 / PR-E2 概要

| PR | 内容 | base | 依存 |
|---|---|---|---|
| **PR-E1** | feat(avatar): dynamic avatar set transfer (layered mode) — firmware に `AvatarSet` クラス + HTTP fetcher、gateway に `load_avatar_set` MCP tool + capture_server endpoint を追加 | upstream `main` | — |
| **PR-E2** | feat(avatar): matrix mode (90 枚) support — mode 切替対応、matrix mode 描画ロジック | PR-E1 | PR-E1 merge 後または並行 |

詳細な commit 構成は `feature/dynamic-avatar-set` の `git log upstream/main..feature/dynamic-avatar-set` を参照 (現状 9 commit、`scaffold` → `WS handler` → `MCP tool` → `unify face/eyes/mouth` → `defer expression during fetch` 等)。本書 Phase X' 投稿時に commit を論理単位で再整理する。

### 非破壊保証 — 既存の `avatar_images.local.cc` 経路はそのまま動く

upstream の既存 avatar 焼き込み機構 (`avatar_images.{cc,h}` placeholder + `avatar_images.local.{cc,h}` の CMake `STACKCHAN_LOCAL_AVATAR_CC` 差し替え + `firmware/scripts/avatar_convert/convert_avatars.py` の PNG → RGB565 変換、`7c084cd "Support gitignored local avatar overrides"`) は **PR-E1 / PR-E2 で一切変更しない**。

確認済み事実 (2026-05-15):

- `git diff upstream/main..feature/dynamic-avatar-set --stat` で `CMakeLists.txt` / `avatar_images.cc` / `avatar_images.h` / `convert_avatars.py` のいずれも変更なし
- `firmware/main/boards/stackchan/stackchan.cc:1563-1605` の `FaceImageForIndex` / `EyesImageForIndex` / `MouthImageForIndex` は **AvatarSet がロード済み (= `is_loaded() && mode == kLayered`) かつ該当 face/eyes/mouth が AvatarSet 内にあるときだけ AvatarSet を返し、それ以外は `avatar_images.h` の static const table (`avatar_idle` 等) にフォールスルー**。ユーザーが `avatar_images.local.cc` で実 art を焼いていれば、リンク時に local 側が拾われて自動的に static 経路で実 art が描画される

つまり upstream の現行ユーザー (= 静的 art 派、`~/.stackchan/avatar/` に PNG を置いて `convert_avatars.py` でビルド時焼き込み) は、PR-E1/E2 が merge されても **`load_avatar_set` MCP tool を呼ばなければ現状維持で動く**。動的 AvatarSet は「層 3」として上に被さる追加の選択肢で、既存の「層 1: placeholder」「層 2: local static override」を温存する。

### PR description に貼るべき 3 層モデル表

PR-E1 本文で reviewer の初手懸念 (= 既存 local override 機構との競合) を解消するため、`stackchan_avatar_pipeline.md` §B-0 の 3 層モデル表をそのまま転載する:

| 層 | 何 | 何のため | 既存維持 |
|---|---|---|---|
| 1. placeholder | `avatar_images.cc` の 1×1 黒ピクセル | 起動時の保険、AvatarSet 未ロード時の表示 | はい |
| 2. local static override | `avatar_images.local.{cc,h}` (CMake で差し替え) | 静的 art を焼きたいユーザー向け、firmware 焼き直し前提 | はい (本 PR で非破壊) |
| 3. dynamic avatar set | 新規 `AvatarSet` クラス + HTTP fetcher | 動的にロードされる PSRAM 上の art セット、ペルソナ別 / multi-character 対応 | (本 PR で新規追加) |

加えて以下の一文を入れると review コストが減る:

> Existing users of the `avatar_images.local.cc` flow are unaffected: if `load_avatar_set` is never called, `FaceImageForIndex` / `EyesImageForIndex` / `MouthImageForIndex` fall through to the existing static const tables exactly as before.

### デフォルト art は upstream メンテナへ依頼

`avatar_images.cc` placeholder TODO (= `Replace with real 160×120 RGB565 art before shipping to production.`) の解決は **PR では送らない**。デフォルト ｽﾀｯｸﾁｬﾝキャラの art は stackchan-mcp の「顔」になるリソースで、デザイン判断は upstream メンテナ (如月もちさん) が握るべき領域。我々は形式 (layered mode の manifest schema) だけを PR-E1/E2 で整え、「PR-E1/E2 で導入される avatar セット形式に沿ったデフォルト art を作っていただければ placeholder TODO が解決します」と issue/discussion で伝える。

### 投稿条件

- Phase 4.5-a (firmware 拡張) / 4.5-b (gateway MCP tool) / 4.5-c (addon storage + 永続化) / 4.5-d (画像生成 UI) の実機検証完了後
- Phase X' (= 既存 Series A〜D) と並行投稿可能。依存なし

### Series E 用ブランチ分割手順 (Phase 4.5-e 着手時の参考)

```bash
cd temp/stackchan-mcp
git fetch upstream

# PR-E1 = layered mode のみで dynamic transfer 機構を切り出す
git switch -c pr-e1-dynamic-avatar-set-layered upstream/main
# feature/dynamic-avatar-set から layered mode に必要な commit を cherry-pick
# (matrix mode 追加分 = f3a988b "unify face/eyes/mouth via AvatarSet, add matrix mode rendering" は除外)
git cherry-pick 2dcfe5a    # scaffold AvatarSet + HTTP fetcher
git cherry-pick 3bd241c    # wire avatar_set_fetch WS handler + completion notify
git cherry-pick e6ff313    # gateway: load_avatar_set MCP tool + WS fetch protocol
git cherry-pick d9a402c    # fix: expose Protocol::SendText as public for board-initiated WS notify
git cherry-pick a42fe0b    # fix: replace %zu with %u in ESP_LOG (nano-printf compat)
git cherry-pick 5cd11a6    # defer expression changes during avatar set fetch
git cherry-pick 740d786    # refactor: AvatarSet ownership-transfer (PSRAM peak 9.9 → 3.3 MB)
git push origin pr-e1-dynamic-avatar-set-layered

# PR-E2 = matrix mode 追加 (PR-E1 base、Stacked)
git switch -c pr-e2-matrix-mode pr-e1-dynamic-avatar-set-layered
git cherry-pick f3a988b    # unify face/eyes/mouth via AvatarSet, add matrix mode rendering
git push origin pr-e2-matrix-mode
```

CI/build 系の commit (`98f34b0 ci(fork): trigger Build on feature/** + dev/** + workflow_dispatch`、`b4bcdc3 chore(ci): trigger build with default branch updated`) は fork-only の dev infrastructure なので upstream PR には含めない (= Series A〜D と同じ扱い)。

cherry-pick の順序や境界は Phase 4.5-e 着手時の実装状況で再点検する (= ここに書いた commit hash は 2026-05-15 時点)。

### PR-E1 の注意点

- avatar セット転送経路は **HTTP fetch** (既存 capture_server を流用)。既存音声 WS への影響をゼロにする設計判断は intent doc §C で根拠化済み
- firmware は **raw RGB565 のみ** サポート (CONFIG_LV_USE_PNG が unset、OTA partition 圧迫回避)。PNG → RGB565 変換は addon 側で完了させ、gateway には Pillow 等の重い依存を持ち込まない
- 不変条件 #6 (= avatar セット転送中の表情切替コマンドは転送完了まで defer) は実装済み (`5cd11a6 defer expression changes during avatar set fetch`)、PR description でこの設計判断を明示
- 2026-05-18 追加 commit `740d786` で `AvatarSet::Load` (memcpy 版) を `AdoptOwnedBuffer` (所有権譲渡版) に置き換え。 Fetcher の staging buffer を AvatarSet に直接渡すことで内部 memcpy を廃止、 PSRAM peak が (旧 buffer + 新 staging) のみに収まる。 元の `avatar_set_fetcher.cc:73-81` の follow-up TODO 解消、 matrix mode (= PR-E2) で実機発覚した `Load: PSRAM allocation failed (size=3456000)` を解決 (詳細: `docs/issues/stackchan_avatar_psram_peak.md`)

### PR-E2 の注意点

- matrix mode (90 枚) は PSRAM 3.3 MB を消費。8 MB PSRAM の使用上限 5 MB 内で xiaozhi-esp32 base の他用途と共存可能、を実機ログで示す
- mode 切替は avatar セット単位 (= ペルソナ憑依時にセットごと差し替え)、ロード中 mode 変更不可、を doc で明示

## 追補: PR-F — device-driven listen audio capture forwarding (Phase 3' 対応、2026-05-18 追記)

Phase 3' で実装した device-driven listen 音声経路 (詳細設計は `docs/intent/stackchan_vessel.md` v0.7 §C-2 / §G) の upstream PR。Series A〜E とは独立、依存なし。

### PR-F 概要

| PR | 内容 | base | 依存 |
|---|---|---|---|
| **PR-F** | feat(audio): forward device-driven listen captures to an external HTTP hook | upstream `main` | — |

ブランチ: `feature/device-driven-audio-capture-with-hook` (= 4 commit、2026-05-18 時点で fork に push 済み、`dev/integration` へ merge 済み)。

### 投稿条件

- Phase 3' の実機検証完了後 (= 「LCD タッチ起動 → Gemini ペルソナが固有名詞を含む発話を理解して返答」 が確認できた段階)
- Series A〜E と並行投稿可能。 依存なし

### PR description ドラフト

reviewer (= kisaragi-mochi) 向け。 受け入れの分岐ポイント (= server-driven listening モデルとの哲学的整合) を明示する:

> **What**
>
> Forward device-initiated listen captures (wake word, button press, LCD touch — any path that calls `Application::ToggleChatState` / `WakeWordInvoke` / `StartListening` on the firmware) to an externally configured HTTP hook as an Ogg/Opus payload.
>
> **Why**
>
> stackchan-mcp's primary listen model today is MCP-client-driven (the `listen()` tool): the LLM agent decides when to open the device's microphone and the gateway transcribes the resulting Opus stream through a registered STT engine. This works well for "AI agent initiates listening" workflows.
>
> The device-side firmware, inherited from xiaozhi-esp32, also has a reverse path: when a wake word fires, a button is pressed, or (board-dependent) the LCD is tapped, the firmware sends `{"type":"listen","state":"start"}` to the gateway and starts streaming Opus frames. The gateway currently ignores these inbound listen messages, so the frames are dropped at `audio_stream.handle_audio_frame`'s "no active recording slot" branch. The behaviour is documented in the existing comment:
>
> > the device may emit audio on its own (e.g. after an autonomous wake-word detection) and the gateway has no STT pipeline running for those frames yet.
>
> This PR fills that gap for the case where the gateway operator wants to forward those frames to a downstream service — a non-Whisper recognizer, a recorder, or (our use case) a Gemini-powered persona that consumes audio as `inline_data`. It does so without changing the MCP-driven default: the device-driven capture path is enabled only when `STACKCHAN_AUDIO_HOOK_URL` is set.
>
> **How**
>
> 1. **Inbound listen handler** (`esp32_client._handler`): when `STACKCHAN_AUDIO_HOOK_URL` is configured AND no MCP-driven `listen()` is already capturing, open the shared `audio_stream` recording slot on `state="start"` and close it on `state="stop"`. Without the hook URL, the device-driven branch logs at debug and returns immediately — current behaviour preserved.
> 2. **Ogg/Opus packing** (`audio_input_hook.pack_opus_frames_to_ogg`): pure-Python RFC 7845 + RFC 3533. No new runtime dependencies — the existing `opuslib` is for codec, not container, and we intentionally avoid pulling in `pyogg` for a 200-line wrapper.
> 3. **HTTP push** (`audio_input_hook.push_audio_capture`): aiohttp POST with `Content-Type: audio/ogg`, `Authorization: Bearer <STACKCHAN_AUDIO_HOOK_TOKEN>` (falls back to `STACKCHAN_TOKEN`), and `X-StackChan-Session: <gateway session id>`. Fire-and-forget; failures are logged at WARNING and do not propagate.
> 4. **Disconnect cleanup**: connection close mid-capture drops the partial buffer (mirrors the existing session-mismatch discard logic in `audio_stream.handle_audio_frame`).
>
> **Compatibility**
>
> - **Default OFF / opt-in**: `STACKCHAN_AUDIO_HOOK_URL` unset → inbound listen messages are still logged at debug and discarded, matching today's behaviour.
> - **No conflict with MCP-driven `listen()`**: if an MCP `listen()` has already opened the recording slot, the device-driven branch defers (existing slot is honoured). The shared `audio_stream` module-level singleton remains the single capture buffer.
> - **No new runtime dependencies**.
>
> **Tests**
>
> - `tests/test_audio_input_hook.py` (new, 9 cases): Ogg page structure (BOS / OpusTags / EOS, granule monotonicity, multi-page layout), CRC round-trip (parse, zero, recompute, compare), HTTP push success / empty / 5xx.
> - `tests/test_esp32_client.py` (extended, 3 new cases): device-driven listen → frame → stop → hook fire; hook URL absent → no recording slot; disconnect mid-capture → partial buffer discarded, no hook.
> - All 264 pre-existing tests continue to pass.
>
> **Out of scope**
>
> - Audio recognition itself — this PR ships frames out the door; recognition is the receiver's responsibility.
> - Multiple concurrent hooks / topic routing. One configured URL gets every device-driven capture.

### PR-F の注意点

- 設計範囲を **拡張する** タイプの PR (= 「stackchan-mcp は意図的に server-driven listening を採用」 という maintainer の方針への追加提案)。 PR-B (xiaozhi OTA skip) と同程度かそれ以上に description を厚めに書く必要あり
- 既存 server-driven 経路を壊さないこと (= default OFF、 既存 `listen()` ツールとの排他は recording slot 共有で自然解決) を明示するのが受け入れの分岐ポイント
- 受け入れ拒否の場合: 手元 fork で運用継続。 SAIVerse 側 addon の `mcp_servers.json` は `dev/integration` を指したまま (= Series A〜D / E と同じ運用)
- 関連 Issue: #8 (Phase 4: Opus audio stream、 phase-4-audio + help wanted)、 #91 (= 既に Closed の MCP-driven listen() 実装)、 #169 (= persistent WS + logical audio state 分離、 我々の PR とは別軸だが将来 integration の余地あり)

### Series F 用ブランチ分割手順

PR-F は単一 PR で、 分割不要 (= 4 commit がすでに論理単位で整理済み):

```bash
cd temp/stackchan-mcp
git fetch upstream

git switch -c pr-f-device-driven-audio-capture upstream/main
git cherry-pick 557c49f    # feat(audio): add audio_input_hook for device-driven listen capture push
git cherry-pick 9a7dae5    # feat(esp32_client): handle inbound listen messages for device-driven capture
git cherry-pick c7338a2    # feat(gateway): wire STACKCHAN_AUDIO_HOOK_URL/TOKEN env into ESP32Manager
git cherry-pick 18f0562    # test(audio): cover device-driven listen capture path
git push origin pr-f-device-driven-audio-capture
```

cherry-pick の hash は 2026-05-18 時点。 PR 投稿時に再確認する。

## 追補: PR-G — coredump-to-flash for esp32s3 (Phase 3' デバッグ基盤、 2026-05-18 追記)

stack-chan 系の reset 真因 (= watchdog / panic / stack overflow / brownout) を実機再現後に特定するため、 ESP-IDF の coredump partition を有効化した。 Series A〜F とは独立、 依存なし。

### PR-G 概要

| PR | 内容 | base | 依存 |
|---|---|---|---|
| **PR-G** | feat(firmware/esp32s3): enable coredump-to-flash for panic backtrace retention | upstream `main` | — |

ブランチ: `feature/coredump-partition` (= 1 commit、 2026-05-18 時点で fork に push 済み、 `dev/integration` へ merge 済み)。

### 投稿条件

- Phase 3' の stroke 再現実験で coredump 機能が動作確認できた後 (= 任意の reset 後 `idf.py coredump-info -p <PORT>` で panic backtrace + register state が取れることを実機で確認)
- Series A〜F と並行投稿可能。 依存なし

### PR description ドラフト

> **What**
>
> Add a 64KB `coredump` partition to the v2/16m.csv layout (16MB ESP32-S3 flash) and enable `CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH` in `sdkconfig.defaults.esp32s3`. After a watchdog / panic / stack overflow / brownout the firmware writes a backtrace + register dump to the new partition which survives reboot. `idf.py coredump-info -p <PORT>` retrieves the saved dump for offline analysis.
>
> **Why**
>
> The ESP32-S3 USB CDC peripheral re-enumerates on reset, which causes the host's serial-port reader to lose the boot sequence and any panic backtrace printed during the abort. Without a coredump partition, intermittent resets are hard to root-cause: the device reboots, the host loses sight of the `<panic>` output, and only the post-reboot logs remain. Persisting the dump to flash is the standard ESP-IDF workaround.
>
> **How**
>
> 1. `partitions/v2/16m.csv`: shrink the `assets` partition from 8M to 0x7F0000 (= 8M − 64K) and append `coredump,data,coredump,0xFF0000,0x10000,` at the end of flash. No existing partition offsets change.
> 2. `sdkconfig.defaults.esp32s3`: enable `CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH` + ELF/CRC32 options. ESP32-C3 / ESP32-P4 etc. are unaffected because the configuration is chip-specific.
> 3. `partitions/v2/README.md`: document the new partition under "16MB Flash Devices (Standard)" and add a "Coredump Retrieval" section.
>
> **Compatibility**
>
> - **No existing partition offsets change**: NVS, OTA partitions, and the app stay at the same flash addresses, so `idf.py partition-table-flash` + `idf.py app-flash` preserves WiFi credentials and previously-cached state across the partition table upgrade. Merged-binary flashing is **not** required.
> - **Assets size reduction is negligible**: actual asset usage on stack-chan is ~1.5MB so the new 8128KB `assets` partition has ~6.5MB headroom.
> - **Other chip families unaffected**: coredump sdkconfig entries are gated to esp32s3.
>
> **Tests**
>
> - Manual: trigger an abort on hardware, reboot, verify `idf.py coredump-info -p <PORT>` reproduces the backtrace + register state with source-line mappings.
>
> **Out of scope**
>
> - Higher-tier dumping (full task list, FreeRTOS state) — this PR covers the minimal ELF panic dump. Larger scopes require partition size > 64K and are deferred until the basic dump proves insufficient in practice.

### PR-G の注意点

- `assets` partition の縮小 (8M → 8M − 64K) を含むので、 reviewer に「実 asset 使用量 ~1.5MB」 という実測根拠を PR description で示すこと。 16MB v2 layout はディスク余裕が大きいので影響なし
- 受け入れ拒否されたら、 16m.csv を別ファイル (= `16m_with_coredump.csv` 等) に分離して `sdkconfig.defaults.esp32s3` で上書きするバリアント PR に再構成する余地あり

### Series G 用ブランチ分割手順

PR-G は単一 PR で、 分割不要 (= 1 commit が論理単位として整理済み):

```bash
cd temp/stackchan-mcp
git fetch upstream

git switch -c pr-g-coredump-partition upstream/main
git cherry-pick 079e0c2    # feat(firmware/esp32s3): enable coredump-to-flash for panic backtrace retention
git push origin pr-g-coredump-partition
```

cherry-pick の hash は 2026-05-18 時点。 PR 投稿時に再確認する。

## 参考

- 手元 fork のブランチ:
  - `feature/external-pcm-stream` (= 9 commit が直線、Phase 1' 検証経路として活用中、Series A〜D の出所)
  - `feature/dynamic-avatar-set` (= 10 commit、Phase 4.5 検証経路として活用中、Series E の出所、 2026-05-18 に `740d786` PSRAM peak fix 追加)
  - `feature/device-driven-audio-capture-with-hook` (= 4 commit、Phase 3' 検証経路、 Series F の出所)
  - `feature/coredump-partition` (= 1 commit、Phase 3' デバッグ基盤、 Series G の出所、 2026-05-18 追加)
- addon 側で参照: `expansion_data/saiverse-stackchan-addon/mcp_servers.json` の `--from git+https://github.com/maha0525/stackchan-mcp.git@<branch>#subdirectory=gateway` (現状 `feature/external-pcm-stream`、Phase 4.5 統合時に `dev/integration` に切替)
- upstream: `https://github.com/kisaragi-mochi/stackchan-mcp`
- `docs/intent/stackchan_vessel.md` §「Phase X'」(= 上位概念のスコープ定義)
- `docs/intent/stackchan_avatar_pipeline.md` §B-0 (= 3 層モデル) / §E (= upstream PR ストーリー)
