# Issue: integrate/all-fixes-2026-05-25 ブランチの全 commit と組み込み経路

**ステータス**: 🟢 構築完了、 実機 flash 済 (2026-05-25 22:40 時点)
**優先度**: medium (= 後続セッションで参照される、 仕様 doc 兼 ハンドオフ)
**作成日**: 2026-05-25
**関連**: `docs/issues/stackchan_mcp_upstream_pr_strategy.md` (PR 投稿戦略全体)、`maha0525/stackchan-mcp#integrate/all-fixes-2026-05-25`

## 目的

新統合ブランチ `integrate/all-fixes-2026-05-25` の構成を 1 文書に集約。 過去セッションの引き継ぎ漏れで重要 fix (PR #225 / Si12T align / boot_session_id / coredump / matrix mode) が再三 取り込み欠落していたため、 「どの commit がどの経路で入っているのか」 を見て分かる形にする。

## 構成サマリ

```
base: upstream/main (= d46e78b, 2026-05-24 16:00)
  └─ fork-only 32 commits (= cherry-pick 28 + 独自 2 + a0cf456 等 2 = 計 32)
```

- 全 commit 数: upstream/main 部分 + 32 commits の fork-only
- 直近 build / flash 状態: build clean (xiaozhi.bin 0x317c40 bytes, 21% free)、 COM3 へ app-flash 済 + partition-table-flash 済 (coredump partition 追加分)

## upstream/main 経由で取り込まれる重要 PR (= 直接 cherry-pick していない)

upstream/main を base にしたので、 これらは自動的に新ブランチに含まれる。 fork で先行実装していた相当 commit は `git cherry-pick` 時に空になって skip されるか、 別 SHA で 既に upstream に merge 済み。

| upstream SHA | upstream PR | 内容 |
|---|---|---|
| `d46e78b` | #210 | dynamic AvatarSet transfer pipeline (HTTP fetch + AdoptOwnedBuffer) — PR-E1 |
| `82c322b` | #225 | **keep idle after tts.stop, remove auto-listening recovery** — speaking 終了後の listening 自動復帰を消す本体修正、 巻き戻り症状の根本対処 |
| `564c6a8` | #217 | bundle libopus.dll for Windows + PATH setup — PR-C |
| `1667c4d` | #212 | extract send_pcm_audio helper from synthesize_and_send — PR-A1 |
| `a15b27e` | #223 | Port B WS2812 generic MCP tools |
| `b39f712` | #207 | trigger play_popup_on_listening_ from StartListening() — PR-L |
| `afff815` | #206 | readable touch event log (format bug + per-channel raw decode) — PR-I |
| `b6c4a58` | #219 | StopReconnectTimer guard with atomic flag (hello-then-disconnect race) |
| `9507922` | #205 | fail-fast on invalid server hello |
| `1b8d1d0` | #204 | docs(contributing): Development Philosophy section |
| `ddf4f00` | #186 | wifi first-attempt retry-with-delay (PR-H、 まはー originally) |
| `3c30a12` | #203 | CHANGELOG check CI |
| `3238874` | #202 | I2C MCP tool schemas restrict addr to 0x08..0x77 |
| `3a3569f` | #196 | Port A I2C bus + generic I2C tools (PR-K、 まはー originally) |
| `c6f8a74` | #195 | kPropertyTypeArray (PR-J、 まはー originally) |
| `504f226` | #197 | persistent WebSocket connect on boot + sleep policy |
| `9be82e4` | #192 | session_id-based gating for tts/listen messages |
| `41a22d2` | #136 | keep WebSocket alive when CloseAudioChannel is called |

## fork-only commits (= 新ブランチに cherry-pick / 独自追加した 31 commits)

新ブランチ上の順序 (古→新)。 source SHA は fork の元 commit、 new SHA は cherry-pick 後の新ブランチ上の SHA (cherry-pick で SHA は変わる)。

### Group 1: Si12T sensor 修正 (= 未送 PR-N の素材)

| # | new SHA | source SHA | 内容 | 由来ブランチ | upstream PR 状態 | 取得方法 |
|---|---|---|---|---|---|---|
| 1 | `ecc0358` | `429b259` | align Si12T init with m5stack official driver + L-only filter + raise STROKE_MIN_MS | `dev/integration` | **未送 (= PR-N 候補)** | cherry-pick |

### Group 2: stack-chan touch UX (= PR-M #208 / PR-L #207 の素材)

| # | new SHA | source SHA | 内容 | 由来ブランチ | upstream PR 状態 | 取得方法 |
|---|---|---|---|---|---|---|
| 2 | `62c21b5` | `759508b` | route listening-state touch to StopListening | `feature/stackchan-touch-stop-listening` | PR-M #208 OPEN | cherry-pick |
| 3 | `8f233cf` | `97cd6bd` | instrument touch poll + state machine + LED tap feedback | `debug/stackchan-touch-poll-instrumentation` | PR-M #208 OPEN (= 一部救出、 debug log は upstream に出さない方針) | cherry-pick + conflict resolve (application.cc:730) |
| 4 | `61b73d6` | `397d3bc` | touch tap uses StartListening to force ManualStop mode | `fix/stackchan-touch-uses-startlistening-not-toggle` | PR-M #208 OPEN | cherry-pick |
| 5 | `2c6ca23` | `e13a544` | touch tap sound + debounce + listening timeout | `feature/stackchan-touch-feedback-and-bounds` | PR-M #208 OPEN | cherry-pick |
| 6 | `153377a` | `7efce40` | keep ToggleChatState path for kDeviceStateAudioTesting | `dev/integration-p1-fixes` | PR-M #208 codex review fix | cherry-pick |

### Group 3: PCM stream feature (= PR-A2 #213 の素材)

| # | new SHA | source SHA | 内容 | 由来ブランチ | upstream PR 状態 | 取得方法 |
|---|---|---|---|---|---|---|
| 7 | `7b262d0` | `623a44d` | add send_pcm_stream for incremental PCM push | `pr-a2-send-pcm-stream` | PR-A2 #213 OPEN | cherry-pick |
| 8 | `69338fa` | `6da695a` | docs(changelog) PR-A2 | `pr-a2-send-pcm-stream` | PR-A2 #213 OPEN | cherry-pick |
| 9 | `daf912b` | `0ceebea` | buffer source-rate bytes before resampling | `pr-a2-send-pcm-stream` | PR-A2 #213 codex review fix | cherry-pick |

### Group 4: xiaozhi OTA skip (= PR-B #216 CLOSED の素材、 fork-only で維持必要)

| # | new SHA | source SHA | 内容 | 由来ブランチ | upstream PR 状態 | 取得方法 |
|---|---|---|---|---|---|---|
| 10 | `bcca271` | `3a7fd56` | skip xiaozhi OTA check to keep WebSocket gateway URL configurable | `pr-b-skip-xiaozhi-ota` | **PR-B #216 CLOSED** (= upstream #119 で URL 保護は別経路で達成、 だが fork 側は cloud probe そのものを撤去する判断 = 維持) | cherry-pick |
| 11 | `fa9454c` | `1235364` | preserve OTA rollback confirmation while skipping xiaozhi probe | `pr-b-skip-xiaozhi-ota` | PR-B codex review fix | cherry-pick |
| 12 | `7c781c0` | `ec86831` | docs(changelog) PR-B | `pr-b-skip-xiaozhi-ota` | PR-B #216 CLOSED | cherry-pick + CHANGELOG conflict resolve |

### Group 5: device-driven audio capture (= PR-F #209 の素材)

| # | new SHA | source SHA | 内容 | 由来ブランチ | upstream PR 状態 | 取得方法 |
|---|---|---|---|---|---|---|
| 13 | `2a1f436` | `77eb334` | add audio_input_hook for device-driven listen capture push | `pr-f-device-driven-audio-capture` | PR-F #209 OPEN | cherry-pick |
| 14 | `fe5ccfc` | `39169a1` | esp32_client handle inbound listen messages | `pr-f-device-driven-audio-capture` | PR-F #209 OPEN | cherry-pick + conflict resolve (esp32_client.py:589 で avatar_set_loaded 分岐と並置) |
| 15 | `cb6c86a` | `8db4bf9` | wire STACKCHAN_AUDIO_HOOK_URL/TOKEN env into ESP32Manager | `pr-f-device-driven-audio-capture` | PR-F #209 OPEN | cherry-pick |
| 16 | `09407c1` | `590f8ff` | test(audio) cover device-driven listen capture path | `pr-f-device-driven-audio-capture` | PR-F #209 OPEN | cherry-pick |
| 17 | `68aa1f7` | `2656512` | docs(changelog) Gateway PR-F | `pr-f-device-driven-audio-capture` | PR-F #209 OPEN | cherry-pick + CHANGELOG conflict resolve |
| 18 | `8c7ac73` | `19a0310` | scope device-driven recording cleanup to owning session | `pr-f-device-driven-audio-capture` | PR-F #209 codex review fix | cherry-pick |
| 19 | `7b20e2f` | `e10c5b1` | split opus packets > 255 bytes into Ogg lacing segments | `pr-f-device-driven-audio-capture` | PR-F #209 codex review fix | cherry-pick |
| 20 | `6be6b09` | `1b479d9` | style(tests) remove unused asyncio import | `pr-f-device-driven-audio-capture` | PR-F #209 ruff F401 対応 | cherry-pick |

### Group 6: POST /pcm endpoint (= PR-A3 #214 の素材)

| # | new SHA | source SHA | 内容 | 由来ブランチ | upstream PR 状態 | 取得方法 |
|---|---|---|---|---|---|---|
| 21 | `24a6290` | `445a9ba` | add POST /pcm endpoint for external PCM input | `pr-a3-pcm-endpoint` | PR-A3 #214 OPEN | cherry-pick + conflict resolve (capture_server.py / gateway.py の avatar staging と統合) |
| 22 | `568662a` | `5992a52` | reject non-positive X-Sample-Rate at /pcm boundary | `pr-a3-pcm-endpoint` | PR-A3 #214 codex review fix | cherry-pick |
| 23 | `11f4f11` | `f692719` | docs(changelog) PR-A3 | `pr-a3-pcm-endpoint` | PR-A3 #214 OPEN | cherry-pick |

### Group 7: aiohttp client_max_size (= PR-A4 #215 の素材)

| # | new SHA | source SHA | 内容 | 由来ブランチ | upstream PR 状態 | 取得方法 |
|---|---|---|---|---|---|---|
| 24 | `794e719` | `a0c13e1` | disable aiohttp client_max_size cap on capture app | `pr-a4-client-max-size` | PR-A4 #215 OPEN | cherry-pick |
| 25 | `7ec04c2` | `4a9be45` | per-route body cap on /capture (8 MiB) | `pr-a4-client-max-size` | PR-A4 #215 codex review fix | cherry-pick + conflict resolve (CAPTURE_MAX_BYTES と AvatarStaging を統合) |
| 26 | `222794f` | `8725fbb` | docs(changelog) PR-A4 | `pr-a4-client-max-size` | PR-A4 #215 OPEN | cherry-pick |

### Group 8: coredump partition (= 未送 PR-G の素材)

| # | new SHA | source SHA | 内容 | 由来ブランチ | upstream PR 状態 | 取得方法 |
|---|---|---|---|---|---|---|
| 27 | `af29ed9` | `079e0c2` | enable coredump-to-flash for panic backtrace retention | `feature/coredump-partition` | **未送 (= PR-G 候補)**、 stroke 時 USB CDC re-enumerate 調査用 | cherry-pick |

### Group 9: boot_session_id (= 独立した未送 PR の余地あり)

| # | new SHA | source SHA | 内容 | 由来ブランチ | upstream PR 状態 | 取得方法 |
|---|---|---|---|---|---|---|
| 28 | `5a44af0` | `36f5209` | add boot_session_id to GetDeviceStatusJson for host-side reboot detection | `dev/integration` | **未送 (= 独立 PR 候補)**、 addon avatar_loader が require、 取り込み漏れ事後発見 | cherry-pick |

### Group 10: matrix mode rendering (= PR-E2 #211 の素材)

| # | new SHA | source SHA | 内容 | 由来ブランチ | upstream PR 状態 | 取得方法 |
|---|---|---|---|---|---|---|
| 29 | `11ae0cc` | `2b4b359` | unify face/eyes/mouth via AvatarSet + matrix mode rendering | `pr-e2-matrix-mode` | PR-E2 #211 OPEN | cherry-pick |
| 30 | `3f64a24` | (新規) | lift matrix-mode guard now that E2 rendering is in branch | **独自 commit** (= upstream `b2c2a6d` 「until PR #211」 guard を解除) | upstream PR は不要 (= PR-E2 merge 時に同様の解除が入る) | 独自 |
| 31 | `38d7180` | `a0cf456` | restore PendingAvatarState declarations dropped during matrix-mode refactor | `pr-e2-matrix-mode` | PR-E2 #211 build fix | cherry-pick |

### Group 11: opus.dll bundle (= fork-only な運用都合対応)

| # | new SHA | source SHA | 内容 | 由来ブランチ | upstream PR 状態 | 取得方法 |
|---|---|---|---|---|---|---|
| 32 | `467641a` | (新規) | bundle opus.dll for git-URL uvx install runtime (= PR-C 設計は PyPI wheel に opus.dll を bundle するが、 mcp_servers.json は `uvx --from git+...` で fork を source build 経由で install するため _libs/opus.dll が来ない問題回避。 vcpkg-built opus.dll を SHA256 verified の上で `git add -f` で fork のみ tracked。 License: BSD-3-clause、 `gateway/LICENSE-THIRD-PARTY` で attribution 確保済) | **独自 commit** (= upstream PR-C の git tracked 禁止方針に逸脱、 fork のみで) | upstream PR は出さない (= PR-C 方針尊重) | 独自 |

## skip した commit (= 取り込まなくて済んだもの)

調査の結果、 以下の commit は 「upstream/main に同等改修済み」 か 「依存先に既に含まれている」 ため cherry-pick 不要だった:

| skip した commit | 理由 |
|---|---|
| `47f09ac` (cancel in-flight connect on attempt timeout、 feature/fix-wifi-first-attempt-comeback-timer) | cherry-pick 結果が空 = upstream #186 `ddf4f00` で同等改修が入っているため |
| `cf57fbe` (validate avatar_set fetch auth、 dev/integration-p1-fixes) | upstream/main `d46e78b` (= PR #210) に同等改修が squashed merge 済 |
| `b6aade4` (readable touch event log、 dev/integration) | upstream #206 `afff815` で merge 済 |
| `e5f62d4` / `66cbfe7` (touch popup sound、 dev/integration) | upstream #207 `b39f712` で merge 済 (= 別実装、 race-safe 版) |
| `a432e9f` (keep idle after tts.stop、 dev/integration) | upstream #225 `82c322b` で merge 済 |
| `ab3423e` (remove fork opt-in persistent-WS gate) | upstream #197 で persistent WS が直接サポートされたため、 「fork-only opt-in gate 撤去」 commit は不要 |
| `e9a6a4e` (CHANGELOG.md + build.yml sync from upstream/main) | base が upstream/main なので不要 |

## 独自 commit の justification

### `3f64a24` lift matrix-mode guard

upstream `b2c2a6d "reject matrix-mode avatar loads until PR #211 wires render"` は PR-E1 (#210) の codex review fix。 matrix render が wire されていないので明示的に reject する目的だった。 PR-E2 (#211) で matrix render を wire したので、 この guard を外す必要がある。

PR-E2 #211 がいずれ rebase + merge される時、 upstream maintainer 側でこの guard 解除が含まれる commit が入るはずだが、 統合ブランチでは先回りで解除する。 commit message に経緯を明示。

## 取り込み漏れの履歴 (= なぜこの doc が必要だったか)

統合ブランチ作成時、 「未統合 feature ブランチを洗う」 段階で 以下 3 件が取り込み漏れていた:

| commit | 症状 | 検知契機 |
|---|---|---|
| `36f5209` boot_session_id | addon avatar_loader が device reboot 検知に必要なフィールド取得失敗 | backend.log `_fetch_device_session_id: no boot_session_id field` WARNING |
| `079e0c2` coredump partition | panic backtrace を flash に保存できない (= stroke 時 USB 切れ調査が困難) | まはー の要求 (= 「stroke 時 USB 切れ調査用に必要」) |
| `2b4b359` + `a0cf456` matrix mode rendering | matrix mode avatar set load が `matrix_mode_unsupported` で常に reject | backend.log `load FAILED ... matrix_mode_unsupported` WARNING |
| `467641a` opus.dll bundle | send_pcm_stream の opuslib が opus.dll を見つけられず PCM POST 500 = stack-chan から声が出ない | gateway log `Exception: Could not find Opus library`、 まはー の指摘「声が出ない」 |

**根本原因**: 「ある経路でリリースされた commit を整理する時、 そのブランチに直接含まれる independent な commits も同時に取り込まれていることを見落とした」。 例: pr-e2-matrix-mode ブランチには matrix mode commits だけでなく PR-E1 stack の commits も含まれており、 これらを一括 merge する経路で取り込みする想定だった改修が 個別 cherry-pick 戦略では抜け落ちる。

**再発防止策**: 統合ブランチ作成後、 必ず 以下を確認:
1. `git log dev/integration --not <new-integrate-branch> --oneline` で漏れ commit を網羅チェック
2. addon (`saiverse-stackchan-addon`) が呼ぶ全機能 (avatar_set load / boot_session_id 取得 / pcm push / device-driven audio capture / etc) を実機 + backend.log の WARNING / ERROR で動作確認
3. 実機 flash 直後の boot で「初回 vessel entry → avatar load 成功」 まで監視

## 関連ドキュメント

- `docs/issues/stackchan_mcp_upstream_pr_strategy.md`: PR 投稿戦略全体 (= 各 PR の状態、 fold-in vs defer の判断、 review 対応履歴)
- `docs/intent/stackchan_vessel.md`: Vessel 駆動 UX の上位概念
- `docs/intent/stackchan_avatar_pipeline.md`: 動的 avatar セット転送の設計 (= 3 層モデル / PR-E1/E2 の役割分担)
- `expansion_data/saiverse-stackchan-addon/mcp_servers.json`: gateway の `--from git+...@<branch>` 参照 (現状 `integrate/all-fixes-2026-05-25`)

## 参考 link

- 新ブランチ: <https://github.com/maha0525/stackchan-mcp/tree/integrate/all-fixes-2026-05-25>
- upstream/main: <https://github.com/kisaragi-mochi/stackchan-mcp/tree/main>
- fork repo: <https://github.com/maha0525/stackchan-mcp>
