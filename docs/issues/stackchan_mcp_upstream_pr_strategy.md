# Issue: stackchan-mcp upstream への PR 投稿戦略 (Phase X')

**ステータス**: 🟡 大半が review 中 (= PR-J/K #195/#196 + PR-H #186 が merged、 残 Series A〜F + PR-B/C + Series E/L/M が 2026-05-21 一斉投稿済み = #207〜#217、 PR-G coredump のみ未送)
**優先度**: medium
**作成日**: 2026-05-13
**最終更新**: 2026-05-21 (PR-A1〜A4 / PR-B / PR-C / PR-E1/E2 / PR-F / PR-I / PR-L/M を一斉投稿 = #206〜#217、 PR-D は upstream で吸収済 = 不要、 PR-G のみ未送)
**関連**: `docs/intent/stackchan_vessel.md` §「Phase X'」、`docs/intent/stackchan_avatar_pipeline.md` §E、`maha0525/stackchan-mcp` fork branches `feature/external-pcm-stream` / `feature/dynamic-avatar-set`

## 背景

Phase 1' 完了時点で、fork (= `maha0525/stackchan-mcp`) の `feature/external-pcm-stream` ブランチに **9 commit が直線的に重なった状態**になっている。Phase X' で upstream (`kisaragi-mochi/stackchan-mcp`) に PR 投稿するには、これを **論理単位で分割した個別 PR (= Stacked PR 含む)** に整理する必要がある。1 つの巨大 PR にすると reviewer 負担大、merge も all-or-nothing で動かなくなる。

このドキュメントは「どの commit をどの PR にまとめ、どんな base 関係で出すか」「ブランチ分割の具体手順」を残す。

## 現在の commit 配列 (= `feature/external-pcm-stream` HEAD → root 方向)

| # | commit | 内容 | 論理 PR | 依存 |
|---|---|---|---|---|
| 9 | `5e1042a` | `client_max_size=0` で aiohttp streaming body cap 撤去 | **PR7** | PR3 (= `POST /pcm` を導入してる) |
| 8 | `93435a6` | persistent WS connection — NVS flag `websocket.persistent` opt-in (起動時 OpenAudioChannel + 失敗時 ScheduleReconnect) | ~~**PR6b**~~ (upstream #169/#197 で代替、 2026-05-20 取り下げ) | PR6a (= `intentional_close_` bug fix) |
| 7 | `f8927b8` | `intentional_close_` flag を OpenAudioChannelInternal 失敗パスでクリア | ~~**PR6a**~~ (upstream #197 で同等修正、 2026-05-20 取り下げ) | 独立 (pure bug fix) |
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

| PR | 内容 | base | 依存 | 状態 |
|---|---|---|---|---|
| **PR #A1** = [#212](https://github.com/kisaragi-mochi/stackchan-mcp/pull/212) | refactor(tts): extract `send_pcm_audio` from `synthesize_and_send` | upstream `main` | — | 投稿済 (2026-05-21) |
| **PR #A2** = [#213](https://github.com/kisaragi-mochi/stackchan-mcp/pull/213) | feat(tts): `send_pcm_stream` for incremental PCM push | PR #A1 | PR #A1 と stacked | 投稿済 (2026-05-21) |
| **PR #A3** = [#214](https://github.com/kisaragi-mochi/stackchan-mcp/pull/214) | feat(capture_server): `POST /pcm` endpoint for external PCM input | PR #A2 | PR #A2 と stacked | 投稿済 (2026-05-21) |
| **PR #A4** = [#215](https://github.com/kisaragi-mochi/stackchan-mcp/pull/215) | fix(capture_server): disable aiohttp `client_max_size` cap | PR #A3 | PR #A3 と stacked | 投稿済 (2026-05-21) |
| **PR #B** = [#216](https://github.com/kisaragi-mochi/stackchan-mcp/pull/216) | fix(firmware): skip xiaozhi-cloud OTA check (NVS websocket.url 保護) | upstream `main` | — | 投稿済 (2026-05-21) |
| **PR #C** = [#217](https://github.com/kisaragi-mochi/stackchan-mcp/pull/217) | fix(gateway): bundle libopus.dll for Windows + DLL search path setup | upstream `main` | — | 投稿済 (2026-05-21) |
| ~~PR #D~~ | ~~feat(firmware): opt-in persistent WS connection + reconnect bug fix~~ | — | — | **不要** (= upstream #169/#197 で吸収済、 dev/integration の `ab3423e` でも opt-in gate を撤去済) |

= **計 6 PR** (Phase 1' 由来、 音声経路 + OTA + libopus)。Series A の 4 PR は stacked、 B/C は独立。 PR-D は upstream merge で目的達成済のため取り下げ。

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

PR ごとに maintainer とディスカッションが入る前提で、各 PR review-cycle に 1-2 週間想定、全 PR merge には 1-3 ヶ月かかる可能性がある。

### 方針の例外 (2026-05-19 追記)

「全 Phase 完了後にまとめて投稿」 は **当該 Phase 検証で副次的に発見した独立 bug** には適用しない。 具体例:

- **PR-H** (= Wi-Fi captive portal 1 回目失敗 fix): Phase 2' (= ペアリング UX) の実機検証中に発見。 既存 Phase X' スコープ (= Series A〜G) と無関係、 修正は局所的、 検証も済んでいるため即時 PR 投稿が筋
- 判断軸: (1) 既存 Series の commit と無関係 (= base が独立)、 (2) 修正範囲が局所、 (3) 実機 Before/After log が揃ってる、 の 3 つを満たすなら例外として即時投稿可

「実機で本当に有用だった」 を示すための「Phase 全部終わってから」 制約は、 Series A〜G の **音声経路 / avatar / coredump 等の構造的変更** に対する制約で、 局所 bug fix を留め置く根拠にはならない。 後者は早く投稿するほど upstream ユーザー全員が恩恵を受ける。

## 残課題 (Phase X' 実施時に解決すべき)

- **PR #C のバイナリ調達経路**: 現状の PyOgg 由来は暫定。CI ビルドへの切替 (= vcpkg + windows-latest workflow) を実装する PR を併走で提出する形が望ましい
- **Stacked PR の運用**: GitHub の Stacked PR ツール (= `Graphite` 等) を使うか、PR description で base を明示するだけかは upstream maintainer の好み次第
- **テスト追加の範囲**: A2/A3 で test を増やすコミットは別 commit にして PR に追加する (= 既存 commit のままだと test が無いのが目立つ)
- **`SOURCES.md` の更新**: PR #C 提出前に「CI build に置き換え予定」を補強する記述を入れておくと merge しやすい

## 追補: Series E — 動的 avatar セット転送機構 (Phase 4.5-e、 2026-05-21 投稿: [#210](https://github.com/kisaragi-mochi/stackchan-mcp/pull/210) / [#211](https://github.com/kisaragi-mochi/stackchan-mcp/pull/211))

Phase 4.5 で新規に立てた intent doc (`docs/intent/stackchan_avatar_pipeline.md`) の作業ブランチ `feature/dynamic-avatar-set` から、upstream に **2 PR** を投稿する。Series A〜D とは別系統 (= avatar 描画基盤の拡張) で、依存関係も独立。

### PR-E1 / PR-E2 概要

| PR | 内容 | base | 依存 |
|---|---|---|---|
| **PR-E1** = [#210](https://github.com/kisaragi-mochi/stackchan-mcp/pull/210) | feat(avatar): dynamic avatar set transfer (layered mode) — firmware に `AvatarSet` クラス + HTTP fetcher、gateway に `load_avatar_set` MCP tool + capture_server endpoint を追加 | upstream `main` | — |
| **PR-E2** = [#211](https://github.com/kisaragi-mochi/stackchan-mcp/pull/211) | feat(avatar): matrix mode (90 枚) support — mode 切替対応、matrix mode 描画ロジック | PR-E1 | PR-E1 merge 後または並行 |

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

## 追補: PR-F — device-driven listen audio capture forwarding (Phase 3' 対応、 2026-05-21 投稿: [#209](https://github.com/kisaragi-mochi/stackchan-mcp/pull/209))

Phase 3' で実装した device-driven listen 音声経路 (詳細設計は `docs/intent/stackchan_vessel.md` v0.7 §C-2 / §G) の upstream PR。Series A〜E とは独立、依存なし。

### PR-F 概要

| PR | 内容 | base | 依存 |
|---|---|---|---|
| **PR-F** = [#209](https://github.com/kisaragi-mochi/stackchan-mcp/pull/209) | feat(audio): forward device-driven listen captures to an external HTTP hook | upstream `main` | — |

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

## 追補: PR-H — Wi-Fi captive portal 1 回目失敗 fix (Phase 2' 派生、 2026-05-19 投稿済み)

Phase 2' (= ペアリング UX) の実機検証中に発見した bug への即時 PR。 § 投稿タイミング §「方針の例外」 節に従い、 Series A〜G と独立して先行投稿した **本 doc 上初の実投稿 PR**。

### PR-H 概要

| PR | URL | 状態 | base | 依存 |
|---|---|---|---|---|
| **PR #186** ([upstream link](https://github.com/kisaragi-mochi/stackchan-mcp/pull/186)) | https://github.com/kisaragi-mochi/stackchan-mcp/pull/186 | review 1 round → follow-up 投稿、 再 review 待ち (2026-05-20) | upstream `main` (投稿時 `b0e258f`、 投稿後 maintainer 側で `aa01e50` / `37560e9` の 2 回 main merge) | — |

ブランチ: `feature/fix-wifi-first-attempt-comeback-timer` (= 2 commit: 本体 `72fb370` + follow-up `47f09ac`、 fork に push 済み)。 対応 commit は dev/integration の `6b4fa53`。

### 内容

**Symptom**: captive portal の SSID/Password 入力 1 回目が必ず失敗、 同値で 2 回目入力で通る。

**Root cause**: APSTA mode の CSA (Channel Switch Announcement) 直後の association 試行に対して AP が「Association Response status=30 (Refused Temporarily) + Comeback Time 1124 TUs (≈1.1s)」 を返す、 ESP-IDF wifi driver の `failure_retry_cnt` (= 即時 retry) は Comeback Time を尊重しないため全 refuse される。 2 回目試行 (8s 後) は AP state が settle して一発成功。

**Fix**: `WifiConfigurationAp::ConnectToWifi` を上位層 retry loop で囲み、 失敗時に 3 秒 wait → 1 回 retry。 driver-internal `failure_retry_cnt` は default (1) のまま維持。

### 投稿時の知見 (= 他 PR の参考に)

PR description / commit message の構造で reviewer に効いた要素:

- **Symptom** を先頭に置く: 「ユーザーが見える現象」 を最初に書く、 技術的詳細は後
- **Root cause** に観測 log を引用: 「`Association refused temporarily, comeback time 1124 TUs`」 のような生 log を引用すると「推測じゃなく実測」 が伝わる
- **Before/After log** を Code block で並置: 修正前と修正後の挙動を同じフォーマットで見せる、 reviewer が差分を 5 秒で把握できる
- **Limitations** を明示: 「Buffalo router only で検証」 等の制約を隠さない、 受け入れ条件の交渉が楽になる
- **Test plan の checkbox**: `[x]` で済んだ項目と `[ ]` で残った項目 (= 「他 router での検証は maintainer / community に委ねる」) を区別、 review コストが減る

これらの構造は Series A〜G の PR description 作成時にも適用する。 特に PR-B (= xiaozhi OTA skip)、 PR-F (= device-driven audio capture)、 PR-G (= coredump-to-flash) のような「設計範囲を拡張する」 タイプの PR では Symptom / Root cause / Limitations の厚みが受け入れの分岐ポイント。

### Series H 用ブランチ分割手順 (= 今回実施した手順、 他 PR でも踏襲)

```bash
cd temp/stackchan-mcp

# 1. dev/integration に修正を commit (= 手元検証状態を保存)
git switch dev/integration
git add <修正ファイル>
git commit -m "fix(...): ..."   # HEREDOC で詳細な本文も含める

# 2. upstream/main を最新化
git fetch upstream

# 3. feature branch を upstream/main から派生
git switch -c feature/fix-wifi-first-attempt-comeback-timer upstream/main

# 4. dev/integration の commit を cherry-pick (= conflict 出たら手動 resolve)
git cherry-pick 6b4fa53

# 5. fork (origin) に push
git push origin feature/fix-wifi-first-attempt-comeback-timer

# 6. gh CLI で PR 作成 (= base は upstream main、 head は fork の feature branch)
gh pr create --repo kisaragi-mochi/stackchan-mcp \
  --base main \
  --head maha0525:feature/fix-wifi-first-attempt-comeback-timer \
  --title "..." \
  --body "$(cat <<'EOF'
... (Symptom / Root cause / Fix / Verification / Limitations / Test plan)
EOF
)"
```

cherry-pick で `Auto-merging firmware/components/78__esp-wifi-connect/wifi_configuration_ap.cc` が出ても conflict なしなら自動で merge される (= 今回はそのまま通った)。 conflict 出たら手動 resolve + `git cherry-pick --continue`。

### Review 対応履歴

**2026-05-20**: kisaragi-mochi さんから review 着信、 内容は 2 点。

1. **Timeout 分岐で in-flight connect を cancel してない (要修正)**: `xEventGroupWaitBits` の戻り方は 3 経路ある — ① `WIFI_CONNECTED_BIT` (= success)、 ② `WIFI_FAIL_BIT` (= `WIFI_EVENT_STA_DISCONNECTED` 発火済み、 driver は disconnected state)、 ③ timeout (= `bits == 0`、 driver が `connecting` state のまま)。 ESP-IDF v5.5.4 `esp_wifi.h` attention 3 (`esp_wifi_connect()`) に「connecting/scanning state で呼ぶと `ESP_ERR_WIFI_STATE` を返す」 と明記されており、 ③ のままでは slow / event 落ち AP で retry が空振りする可能性。

   → follow-up commit `47f09ac` で対応: `bits == 0` のときだけ `esp_wifi_disconnect()` を呼ぶ + `ESP_LOGW` を timeout / disconnect-fail で区別。 ローカル build (ESP-IDF v5.5.4, M5Stack CoreS3 / stackchan target) は通過 (`idf.py build` exit=0、 `xiaozhi.bin` 生成確認)。 実機 timeout 経路は手元 Buffalo AP では再現できない (常に `WIFI_FAIL_BIT` 経由で返る) ため未検証、 PR comment にその旨を明記した。

2. **Upstream sync (78/esp-wifi-connect) の mirror オファー**: 該当 component `firmware/components/78__esp-wifi-connect/` は [78/esp-wifi-connect](https://github.com/78/esp-wifi-connect) (xiaozhi-esp32 ecosystem) の vendor copy (ESP Component Manager 経由、 `~3.1.3`)。 kisaragi-mochi さんから「PR #186 merge 後に upstream PR を mirror する、 authoring credit はまはー (`--author` 設定 + PR description に `Originally contributed by @maha0525 in kisaragi-mochi/stackchan-mcp#186` を明記)」 と申し出。

   → オファー受諾、 PR comment で返信済み。 78/esp-wifi-connect 側の PR は #186 merge 後に kisaragi-mochi さん経由で出る、 まはー側の追加作業なし (新規 fork / branch も不要)。

### 結果待ち

PR #186 の merge を待ち、 merge されたら本セクションに結果を追記。 78/esp-wifi-connect 側の mirror PR も追跡対象 (= kisaragi-mochi さんから link 通知が来たらここに記録)。

## 追補: PR-I — touch event log readability (= 元の false positive filter スコープから縮減、 2026-05-21 投稿: [#206](https://github.com/kisaragi-mochi/stackchan-mcp/pull/206))

Phase 2' 検証中に副次発見した「触ってないのに STROKE event」 誤検知への対処。 詳細観測 + 仮説 + 解決案候補は `docs/issues/stackchan_touch_false_stroke_events.md` を参照。 Series A〜H と独立、 依存なし。

### PR-I 概要

| PR | 内容 | base | 依存 |
|---|---|---|---|
| **PR-I** | fix(firmware/touch): raw threshold filter for false-positive STROKE events + `duration=lums` format bug | upstream `main` | — |

ブランチ: **未作成** (= 調査 TODO 完了後に派生予定)、 該当 issue (`stackchan_touch_false_stroke_events.md`) の 5 項目を消化してから着手。

### 投稿条件

- `stackchan_touch_false_stroke_events.md` の調査 TODO 全項目消化 (= 特に raw 値の意味 + zone 判定機構の確認)
- 解決案 (1) の threshold をローカル fork で実装 + 24 時間 capture で誤検知 0 件確認
- Phase 5' (= タッチ知覚の本格実装) 着手前に投稿しておくと、 Phase 5' 検証で「ペルソナが触られたと誤認」 のノイズを減らせる

### PR スコープ

issue doc の解決案候補のうち:

- **(1) raw 値 threshold filter** + **(2) `duration=lums` format bug 修正** → 1 PR にまとめる (= 関連箇所が近い、 raw 値判定とログ format は同じ関数内)
- **(3) zone 判定機構の調査** → 調査結果次第で別 PR、 もしくは PR-I に含める

### PR description ドラフト (= 着手時の参考)

issue doc の「観測」 「6 event の log」 「観測差分」 セクションをそのまま PR description に転載できる構造。 PR-H で得た「Symptom / Root cause / Fix / Verification / Limitations / Test plan」 の 6 ブロック構造を踏襲する:

- **Symptom**: 触ってない時に STROKE event が間欠的 (= 約 24 分間隔) に発火する
- **Root cause**: raw 値の threshold 判定が存在せず、 sensor ノイズ起因の高 raw 値が STROKE 判定を通る
- **Fix**: raw 値 threshold filter (= 0x800 〜 0xC00) を touch event 判定前に挿入
- **Verification**: 24 時間 capture で誤検知 0 件、 正常な撫で event は全て検出される
- **Limitations**: 観測は単一個体での結果、 個体差で threshold 調整が必要な可能性 (= configurable に)
- **Test plan**: ローカル flash 後 24 時間 capture / 撫で 10 回 / 触らず 1 時間 etc.

## 追補: PR-J/K — kPropertyTypeArray + Port A I2C generic tools (拡張モジュール対応の第一弾、 2026-05-20 投稿 → 同日 merged)

`docs/intent/stackchan_extension_modules.md` の C 案 (= 汎用口 + 個別 Unit プリセット + addon ドライバの 3 段階) のうち、 **汎用口の upstream PR** を 2 段に分けて投稿した。 stackchan-mcp 本家への fork からの PR で、 dev/integration で実機検証済 (= ENV III 経由)。

### PR-J/K 概要

| PR | URL | 状態 | base | 依存 |
|---|---|---|---|---|
| **PR #195** | <https://github.com/kisaragi-mochi/stackchan-mcp/pull/195> | **merged** (2026-05-20、 merge commit `c6f8a74`) | upstream `main` | — |
| **PR #196** | <https://github.com/kisaragi-mochi/stackchan-mcp/pull/196> | **merged** (2026-05-20、 merge commit `3a3569f`) | upstream `main` | PR #195 |

ブランチ:
- `feature/mcp-property-array-type` (= PR #195) HEAD `7720940`
- `feature/mcp-i2c-generic-tools` (= PR #196) HEAD `c808fa2`

### 内容

**PR #195**: `firmware/main/mcp_server.{h,cc}` に `kPropertyTypeArray` (`PropertyElementType` で Integer / String の要素型) を追加。 既存 Boolean / Integer / String 型と並列、 純粋追加で既存 tool へ影響ゼロ。 `maxItems` cap (= `set_max_items` setter で post-construct 設定) と template default-value constructor の `kPropertyTypeArray` reject も含む (= 後者は review fold-in、 future tool author trap 回避)。

**PR #196**: `firmware/main/boards/stackchan/{config.h, stackchan.cc}` に Port A I2C bus init (= I2C controller 0、 GPIO 2 SDA / GPIO 1 SCL) を追加し、 4 つの MCP tool (`self.i2c.scan` / `read` / `write` / `write_read`) を expose。 internal-IC bus (= I2C controller 1、 GPIO 12/11) との物理 controller 分離で「security by construction」 (= PMU 等の internal IC は構造的に到達不可)。 review で指摘された addr range 0x08..0x77 制約 (= Recommended) と bytes / write_bytes の 256 cap (= Suggested) も fold-in。

### 検証

ENV III (SHT30 0x44 + QMP6988 0x70) を Port A 物理接続して動作確認 (詳細: `docs/issues/stackchan_mcp_i2c_generic_tools.md` §「検証シナリオ」)。

- scan で 0x44 + 0x70 を検出、 internal IC は見えない
- QMP6988 chip ID = 0x5C (= write_read pattern)
- SHT30 温度 32.07°C / 湿度 46.58% (= write + 15 ms wait + read pattern。 single write_read だと measurement 中 0xFF sentinel)
- QMP6988 気圧 1010.4 hPa (= calibration register parse + Q-format compensation 経路、 M5Unit-ENV C 実装の Python 移植 と同値)

### Deferred items (= upstream issue 化)

review で挙げられた `Suggested` 3 件のうち、 fold-in しなかった 2 件は upstream issue として記録 (= future contributor が拾える形に):

| Issue | 内容 | 由来 | Defer 理由 |
|---|---|---|---|
| **#200** | integer-array Property が decimal JSON を `valueint` で int truncate | PR #195 review item 1 | 現状 I2C bytes 用途は整数のみ、 将来 angle / PWM / coord 系で発火 |
| **#201** | `i2c.scan` が main MCP task を最大 ~22 s hold | PR #196 review item 2 | 設計改修 scope (timeout 調整 / partial result / off-task probe)、 correctness 修正 先行 |

### Review への learning (= 今後の PR に活かす)

kisaragi-mochi さん review が **Conventional Comments 形式** (`Suggested` / `Recommended for landing`) で、 fold-in vs defer の判断ラインが明確だった。 今後の Series A-G PR 投稿でも:

- description / response で `Suggested` / `Recommended` の framing を使う (= reviewer / contributor の判断コスト低下)
- defer する item は review への fold-in 返信で「intentionally deferred、 理由 X」 を明示
- defer item は upstream issue 化して可視化 (= 「私が見落とした」 と「明示的に defer した」 を区別できる)

を採用する。

## 追補: PR-L/M — Stack-chan touch-driven listen UX (Phase 3' Vessel 駆動、 2026-05-21 投稿: [#207](https://github.com/kisaragi-mochi/stackchan-mcp/pull/207) / [#208](https://github.com/kisaragi-mochi/stackchan-mcp/pull/208))

Phase 3' (= device-driven audio capture push) の実機検証で、 タッチ操作 → 発話 → タッチ送信の Vessel UX を成立させるために stack-chan board と `Application` 周辺に複数の改修を入れた。 元の xiaozhi-esp32 は「voice assistant 連続会話モデル」 (= 発話後自動で listening 復帰、 タッチは server-driven listen の trigger) を前提にしていたが、 SAIVerse Vessel では「ユーザがタッチして話す → タッチで終了」 を明示的な単発操作として扱うため、 listen 起動 / 終了 / フィードバックの経路を組み替える必要があった。

### dev/integration に積んだ commit (2026-05-19〜21)

| # | commit | 内容 | 想定 PR | 備考 |
|---|---|---|---|---|
| 1 | `759508b` | listening 中の LCD タップを `Application::CloseAudioChannel()` (= 既定 ToggleChatState) ではなく `Application::StopListening()` に分岐させ、 gateway の audio_input_hook 経路 (PR-F 系) で buffer を flush できるようにする | **PR-M** | stack-chan board 限定 |
| 2 | `97cd6bd` | PollTouchpad + `Application::ToggleChatState` / `StartListening` / `StopListening` / `HandleStateChangedEvent` に ESP_LOGI、 板上 RGB LED で touch state visual feedback (= 緑点灯 / 消灯) | **PR-M に部分救出** | 観測 log は PR に入れない、 LED feedback と SetAllRgbLeds helper だけ救出 |
| 3 | `397d3bc` | PollTouchpad の listen 起動を `ToggleChatState()` から `StartListening()` に変更。 前者は `SetListeningMode(AutoStop)` を使うため tts.stop 後に device が自動で Listening 再復帰してしまい、 「タッチ駆動」 が破綻 (= 次のタッチが listen.stop = 即送信)。 後者は `HandleStartListeningEvent` で `SetListeningMode(ManualStop)` を強制する経路に乗り、 tts.stop 後は Idle に留まる | **PR-M** | stack-chan board 限定 (PollTouchpad のみ) |
| 4 | `e13a544` | タッチ瞬時の `Application::PlaySound(OGG_POPUP)` 直接呼び出し (= 後に撤回、 #5 参照) + デバウンス (前回 release から 300ms 以内の press を ignore) + listening タイムアウト (30 秒滞在で auto StopListening) + format bug fix (`%lld` → `%d` cast、 nano-printf 互換) | **PR-M** | デバウンス / タイムアウトは stack-chan board 内、 format fix は副次的改善 |
| 5 | `e5f62d4` | `Application::StartListening()` 内で `play_popup_on_listening_ = true` を立て、 `HandleStateChangedEvent` の `kDeviceStateListening` 分岐内 ResetDecoder 後 PlaySound 経路 (line 980 付近) に乗せる。 既存実装は WakeWord 経路でしか flag 化されていなかったので、 タッチ / API 経由の StartListening では音が鳴らなかった。 同時に board 側の `app.PlaySound(...)` 直接呼びを削除 (= ResetDecoder で playback queue クリアされて消える呼び出し) + `#include "assets/lang_config.h"` 撤去 | **PR-L (Application 部) + PR-M (board 部)** | application.cc 修正は他 board / API 利用者にも有益 |

### PR 分割案

| PR | 内容 | base | 依存 |
|---|---|---|---|
| **PR-L** = [#207](https://github.com/kisaragi-mochi/stackchan-mcp/pull/207) | feat(application): trigger popup-on-listening flag from `StartListening()` so non-wake-word listen activations also get the OGG_POPUP cue | upstream `main` | — |
| **PR-M** = [#208](https://github.com/kisaragi-mochi/stackchan-mcp/pull/208) | feat(firmware/stackchan): touch-driven listen UX (StopListening on listening-state tap, StartListening for activation, RGB LED feedback, debounce, listening timeout, nano-printf format fix) | upstream `main` | PR-L が merge されると board 側の音フィードバックが自動的に効く (= Stacked にしないが PR-L 先行が望ましい) |

**PR-L (Application 単体修正)**:

- 変更ファイル: `firmware/main/application.cc` 1 ファイル
- 1 行追加 (`play_popup_on_listening_ = true;` を `StartListening()` の冒頭に)
- 既存 WakeWord 経路で flag が立っていた仕組みを、 タッチ起動 / Server-driven listen / API 経由 `app.StartListening()` 等すべての activation source で共通に効くように拡張
- xiaozhi-esp32 ecosystem 全体に有用 — 「音声 cue が WakeWord のときだけ鳴る」 は不必要な特殊化
- description: 「StartListening は WakeWord 以外 (= server-driven listen, board-level button / touch) からも呼ばれる public API。 popup cue がそのうち WakeWord ルートでしか鳴らないのは意図しない非対称性で、 ユーザ体感的にも 'listen が始まった' のフィードバックが消える」 を主張

**PR-M (Stack-chan board のタッチ UX 統合)**:

- 変更ファイル: `firmware/main/boards/stackchan/stackchan.cc` 1 ファイル (= board 限定)
- 含む変更:
  - PollTouchpad の listening 中タップを `StopListening()` に分岐 (#1)
  - PollTouchpad の listen 起動を `StartListening()` に (= ManualStop 強制、 自動 listening 復帰回避、 #3)
  - SetAllRgbLeds helper (= set_all_leds MCP tool と同じ I2C 経路を board 内で再利用、 #2/#4)
  - タッチフィードバック: ToggleChatState 分岐 (= listen 起動) で緑点灯、 StopListening 分岐で消灯 (#4)
  - デバウンス: 直前 release から 300ms 以内の press を無視 (#4)
  - listening タイムアウト: state machine listening 突入のエッジ検出 + 30秒経過で auto-StopListening (#4)
  - format fix: `%lld + ms` 連結が nano-printf で format ずれ → `%d` + `(int)cast` (#4)
- description: 「stack-chan は LCD タッチパネル (FT6336) を主操作面とする board で、 voice assistant 連続会話モデル (= 発話後自動 listening 復帰) より 'タッチで開始 / タッチで終了' の single-shot model に振った方が UX が成立する。 他 board の挙動は変えない (= 修正は stack-chan board のみ)」 を主張

### 観測 ESP_LOGI の扱い (= PR には入れない)

`97cd6bd` で入れた以下の ESP_LOGI 群は **PR-L/M に含めない**:

- `Application::ToggleChatState` / `StartListening` / `StopListening` の入口 ESP_LOGI
- `Application::HandleStateChangedEvent` の `State changed -> N` ESP_LOGI
- `StackChanBoard::PollTouchpad` の `FT6336 press / release / short tap -> ...` ESP_LOGI

これらは Phase 3' 切り分け期間の観測用 instrumentation で、 upstream 利用者にはノイズになる。 PR 投稿時の対応:

- 最終的に **撤去** (= 観測完了で目的達成、 出ない方が default)
- または `ESP_LOGD` に降格 (= sdkconfig 経由で開発時のみ visible)

判断軸: 撤去 = upstream の clean さを優先 / LOGD = 将来同種 bug 再現時に sdkconfig 切替で復帰できる利便性。 私の好みは **撤去 + bug 再現時に必要なら別 PR で再投入**。 ただし `Application::HandleStateChangedEvent` の state 遷移 log は xiaozhi-esp32 標準でも `ESP_LOGD` / `ESP_LOGI` 級の有用情報なので、 これだけ LOGI 残しを提案する余地あり。

### 投稿条件

- 当面 dev/integration で運用継続 (= まはー判断 2026-05-21、 「ひとまずこのまましばらく運用してみる」)
- PR-F (= Series F device-driven audio capture forwarding) が merge された後に PR-L/M を投稿するのが自然 (= PR-L/M は PR-F のユースケース下で動く UX を整える PR)。 ただし PR-F 投稿前でも独立 merge 可能 (= 音 cue の対称化 = PR-L、 stack-chan board UX = PR-M はそれ自体で価値)
- Series A〜H と並行投稿可能。 依存なし (PR-L → PR-M の論理依存はあるが、 PR-L 不在でも PR-M 単独で動く = 音 cue が鳴らないだけ)

### PR-L/M 用ブランチ分割手順 (= 投稿時に確認、 hash は 2026-05-21 時点)

```bash
cd temp/stackchan-mcp
git fetch upstream

# PR-L = application.cc 単体
git switch -c pr-l-startlistening-popup-flag upstream/main
# e5f62d4 の application.cc 部分だけ cherry-pick (= board 側の修正は除く)
# 手作業: 該当 hunk を git checkout -p で適用、 もしくは別 commit に分解してから cherry-pick
git push origin pr-l-startlistening-popup-flag

# PR-M = stack-chan board のタッチ UX 統合 (= #1, #3, #4, #5 の board 部分 + LED feedback 救出)
# 観測 ESP_LOGI は cherry-pick 後に追加 commit で撤去 or LOGD 化
git switch -c pr-m-stackchan-touch-driven-listen-ux upstream/main
git cherry-pick 759508b    # touch listening -> StopListening
# 397d3bc の board 部分 (StartListening 経路) は cherry-pick 後 conflict 出る可能性 = e13a544 と境界調整必要
git cherry-pick 397d3bc
git cherry-pick e13a544    # debounce + timeout + LED feedback + format fix
# e5f62d4 の board 部分 (= app.PlaySound 直接呼び削除 + include 整理) を手作業で抽出
# 観測 ESP_LOGI を撤去 or LOGD 化する commit を追加
git push origin pr-m-stackchan-touch-driven-listen-ux
```

cherry-pick の境界 (特に `97cd6bd` の LED feedback 部分の救出 + ESP_LOGI 撤去) は投稿時に再整理する。 1 PR を綺麗にするため、 dev/integration 上の commit を rebase -i で squash + drop した派生ブランチを作る方が clean。

### PR-L/M の注意点

- **PR-L**: 1 行修正だが、 「`StartListening()` を board / API 経由から呼ぶケースがどれだけあるか」 を maintainer が知らない場合は description で具体例を挙げる (= stack-chan の LCD tap、 atk-dnesp32s3-box0 のボタン等、 既存 board 実装で `StartListening()` を呼んでる箇所が複数ある)
- **PR-M**: stack-chan board 限定の変更だが、 description で「voice-assistant model vs single-shot Vessel model」 の設計判断を明示。 受け入れ拒否されたら fork 運用継続 (= memory `feedback_user_experience_first.md`)
- **format fix (`%lld` → `%d` cast)**: nano-printf 制約は他 board / Application でも踏みやすい罠 (= memory `feedback_esp_idf_nano_printf_no_zu.md`、 PR-E1 series `a42fe0b` でも同種修正済み)。 PR-M に含めるが、 description で「ESP-IDF nano-printf compat」 を明示

## 参考

- 手元 fork のブランチ:
  - `feature/external-pcm-stream` (= 9 commit が直線、Phase 1' 検証経路として活用中、Series A〜D の出所)
  - `feature/dynamic-avatar-set` (= 10 commit、Phase 4.5 検証経路として活用中、Series E の出所、 2026-05-18 に `740d786` PSRAM peak fix 追加)
  - `feature/device-driven-audio-capture-with-hook` (= 4 commit、Phase 3' 検証経路、 Series F の出所)
  - `feature/coredump-partition` (= 1 commit、Phase 3' デバッグ基盤、 Series G の出所、 2026-05-18 追加)
  - `feature/fix-wifi-first-attempt-comeback-timer` (= 2 commit: 本体 + 2026-05-20 follow-up `47f09ac`、 Phase 2' 検証経路、 PR-H = #186 の出所)
  - (= PR-I 用ブランチ未作成、 issue 調査完了後に派生)
  - `feature/stackchan-touch-stop-listening` (= 1 commit、 listening 中タップを StopListening に分岐、 Phase 3' UX、 PR-M の commit 出所、 2026-05-19 派生)
  - `debug/stackchan-touch-poll-instrumentation` (= 1 commit、 fork-only 観測ブランチ、 PR には出さない、 LED feedback 部分は PR-M に救出、 2026-05-21 派生)
  - `fix/stackchan-touch-uses-startlistening-not-toggle` (= 1 commit、 listen 起動を StartListening に変更で自動 listening 復帰回避、 PR-M の commit 出所、 2026-05-21 派生)
  - `feature/stackchan-touch-feedback-and-bounds` (= 1 commit、 デバウンス + タイムアウト + LED feedback + format fix、 PR-M の commit 出所、 2026-05-21 派生)
  - `fix/stackchan-touch-popup-sound` (= 1 commit、 StartListening 経由で popup-on-listening flag を立てる、 PR-L + PR-M の commit 出所、 2026-05-21 派生)
- addon 側で参照: `expansion_data/saiverse-stackchan-addon/mcp_servers.json` の `--from git+https://github.com/maha0525/stackchan-mcp.git@<branch>#subdirectory=gateway` (現状 `feature/external-pcm-stream`、Phase 4.5 統合時に `dev/integration` に切替)
- upstream: `https://github.com/kisaragi-mochi/stackchan-mcp`
- `docs/intent/stackchan_vessel.md` §「Phase X'」(= 上位概念のスコープ定義)
- `docs/intent/stackchan_avatar_pipeline.md` §B-0 (= 3 層モデル) / §E (= upstream PR ストーリー)
