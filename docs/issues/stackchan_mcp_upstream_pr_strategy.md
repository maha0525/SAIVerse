# Issue: stackchan-mcp upstream への PR 投稿戦略 (Phase X')

**ステータス**: 🔲 未着手 (= Phase X' 着手前のハンドオフ)
**優先度**: medium
**作成日**: 2026-05-13
**関連**: `docs/intent/stackchan_vessel.md` §「Phase X'」、`maha0525/stackchan-mcp` fork branch `feature/external-pcm-stream`

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

= **計 7 PR**。Series A の 4 PR は順次積み上げ、B/C/D は独立並列で出せる。

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

## 参考

- 手元 fork のブランチ: `feature/external-pcm-stream` (= 9 commit が直線、Phase 1' 検証経路として活用中)
- addon 側で参照: `expansion_data/saiverse-stackchan-addon/mcp_servers.json` の `--from git+https://github.com/maha0525/stackchan-mcp.git@feature/external-pcm-stream#subdirectory=gateway`
- upstream: `https://github.com/kisaragi-mochi/stackchan-mcp`
- `docs/intent/stackchan_vessel.md` §「Phase X'」(= 上位概念のスコープ定義)
