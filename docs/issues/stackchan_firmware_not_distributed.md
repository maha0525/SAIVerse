# Issue: Stack-chan Vessel の firmware が一般ユーザーに届かない

**ステータス**: 🔴 未着手 (= 対応方法の裁定待ち)
**優先度**: high (= 実ユーザーが導入時点で詰まっている)
**作成日**: 2026-09-07
**報告**: 外部ユーザー (Stack-chan Vessel v0.4.0、「ファームウェア書き込み」が利用不可)
**関連**: `docs/intent/stackchan_vessel.md` §不変条件 8 / §I-1、`docs/intent/addon_catalog_management.md` §firmware 配布の方針、`docs/issues/stackchan_mcp_upstream_pr_strategy.md`

## 症状

Stack-chan Vessel を有効化すると AddonManager の UI に警告が出て、「ファームウェア書き込み」が使えない。

> ⚠ merged-binary.bin が見つかりません。「ファームウェア書き込み」は利用不可。
> 配置 path 候補: (1) AddonConfig.firmware_path で絶対 path 指定 (2) `<SAIVerse repo>/temp/stackchan-mcp/firmware/build/` (3) `~/.saiverse/user_data/addon_data/saiverse-stackchan-addon/firmware/`

報告者はリポジトリ内を検索して `expansion_data/saiverse-stackchan-addon/firmware/dist/` に `bootloader.bin` / `partitions.bin` / `firmware.bin` の3つを見つけ、「`merged-binary.bin` の生成または同梱処理が抜けているのではないか」と報告した。

## 実態 — バイナリが抜け落ちたバグではない

`merged-binary.bin` をアドオンに同梱していないのは意図的な設計。device に焼く firmware は `SCServo_lib` 由来で GPL-3.0 であり、SAIVerse 側が再配布すると GPL-3.0 §6 のソース提供義務を SAIVerse が負う。これを避けるため「ユーザーが upstream から直接ダウンロードする」運用にする、と intent の不変条件 8 に定めてある。

**問題は、その「ユーザーが直接ダウンロードする」経路が製品のどこにも存在しないこと。** 内訳は4つある。

### (1) 配布物そのものが存在しない

SAIVerse の Stack-chan Vessel が必要とするのは **fork (`maha0525/stackchan-mcp`) の firmware** であって、upstream (`kisaragi-mochi/stackchan-mcp`) の firmware ではない。upstream にまだ入っていない fork 固有の修正が残っているため。

2026-09-07 時点の実測 (ローカル `temp/stackchan-mcp` を `upstream/main` = 558cf40 / 2026-08-23 と比較):

| ブランチ | upstream/main より先行 | 含まれる firmware 側の修正 (抜粋) |
|---|---|---|
| `integrate/all-fixes-2026-06-24` | 10 commit | WS keepalive (無音切断からの復帰)、ownership lock の per-port 化 |
| `feature/stackchan-imu-readings` | 9 commit | CoreS3 BMI270 の電源と初回サンプル、NFC UID scan、touch 失敗ログの抑制 |

アドオン側には既に IMU の Spell が入っている (アドオン repo の commit `4d42207 Add StackChan IMU spell`)。**upstream の firmware を焼いても、この Spell は動かない。**

そして配布物の置き場所の現状は次のとおり。

| 置き場所 | `merged-binary.bin` | 備考 |
|---|---|---|
| `maha0525/stackchan-mcp` の Releases | **0 件** (リリース自体が存在しない) | fork には CI build がある |
| `kisaragi-mochi/stackchan-mcp` の Releases | あり (`firmware-v1.16.0` 等) | fork 固有の修正は入っていない |
| アドオン repo | 同梱しない方針 | 不変条件 8 |

`kisaragi-mochi` 側は罠も抱えている。`merged-binary.bin` が付いているのは `firmware-*` で始まるタグのリリースだけで、GitHub が「最新リリース」として表示する `v0.17.0` (= gateway 側のリリース) は**アセットが空**。「Releases から落として」と案内された人がページを開くと、一番上のリリースには bin が無い状態になる。

### (2) 入手方法が製品のどこにも書かれていない

- UI の警告 ([`ui/Panel.tsx:968-980`](../../expansion_data/saiverse-stackchan-addon/ui/Panel.tsx)) は**置き場所の候補**しか出さず、どこから入手するかを言わない
- backend の 404 メッセージ ([`api_routes.py:2022-2031`](../../expansion_data/saiverse-stackchan-addon/api_routes.py)) は「GitHub Releases から DL」とだけ書いてあり、**どのリポジトリなのかが無い**
- アドオンの `README.md` には `merged-binary` も `stackchan-mcp` も一度も出てこない

唯一書いてあるのはアドオンカタログ (`registry.json`) の説明文の末尾 (「ESP32-S3 firmware (merged-binary.bin) は GPL-3.0 のため別途手動配置が必要」) だが、これはカタログ画面の説明文であって、警告が出た瞬間に読める場所ではない。

### (3) 隣に紛らわしい旧 firmware が置きっぱなし

報告者が見つけた `firmware/dist/` の3つの bin は、stackchan-mcp を採用する前の**自前 firmware** (2026-05-12 ビルド、`firmware/README.md` に Apache-2.0 と明記、Web Serial で3アドレスに分けて書き込む旧世代の配布形式)。

`docs/intent/stackchan_vessel.md` には `firmware/` を `archive/firmware/` へ移動する計画が書かれているが、**実施されていない**。加えて `README.md` も「Phase 1 + 2 完了 (2026-05-13 時点)」「Web Serial でファームウェアを書き込む」という旧世代の記述のまま。報告者が「これが該当ファイルのはずで、merge 処理が抜けている」と読んだのは、材料がそう読めるように置いてあるため。

### (4) 開発者の環境でだけ常に成功する

`_firmware_resolve_path()` の解決順序の (2) は `<SAIVerse repo>/temp/stackchan-mcp/firmware/build/merged-binary.bin` で、まはーのローカルにはこれが存在する (2026-05-21 build、9.98 MB)。**まはーの環境では警告が一度も出ない。** この経路があるために、配布物が無いことが開発中に露出しない構造になっていた。

## 対応方法

「普通に導入して普通に動く」に到達するには A + B が必須。C は誤誘導の除去、D は再発防止。

### A. fork に firmware のリリースを作る (配布物を用意する)

`maha0525/stackchan-mcp` に firmware タグを切り、`merged-binary.bin` を Releases に publish する。

- 対象ブランチ: 実機で使っている統合ブランチ (現状 `integrate/all-fixes-2026-06-24` + `feature/stackchan-imu-readings` 相当)。**どのブランチを配布版とするかの確定が要る**
- fork には CI build (`workflow_dispatch` 対応) があるので、タグ push → build → Releases 添付の自動化が可能
- upstream 側と紛れないよう、タグ名は SAIVerse 向けと分かる形にする (例: `saiverse-firmware-vX.Y.Z`)

**GPL-3.0 の扱い — ここはまはーの裁定が要る**: バイナリを Releases に置くと、GPL-3.0 §6 のソース提供義務が `maha0525/stackchan-mcp` の配布者に発生する。fork のソースは同じ GitHub 上で公開されているので、リリース説明に「対応するソースはこのタグ」と明記すれば実務上は満たせる形になる (これは私の理解であって法的な確認ではない)。不変条件 8 が禁じているのは「SAIVerse-stackchan-addon が firmware を再配布すること」なので、stackchan-mcp fork から配ること自体は不変条件に抵触しない。ただし**まはーが配布者の立場に立つ**ことを意味するので、そこを承知のうえでの判断になる。

### B. addon.json に `download_file` step を足す (届ける)

**必要な機構は既に実装済みで、addon.json に数行足すだけで済む。**

- `saiverse/addon_installer.py:359` `_exec_download_file()` — URL から DL → SHA256 検証 → atomic rename で配置
- `saiverse/addon_manifest.py:167` `DownloadFileStep` — `url` (HTTPS 必須) / `sha256` (64 hex 必須) / `dest_data` (永続データディレクトリ内の相対 path)
- アドオンは既に `manifest_version: 2` / `setup_version: 1` で、`setup` セクションが空なだけ

書く内容:

```json
"setup": {
    "steps": [
        {
            "type": "download_file",
            "name": "Stack-chan firmware",
            "url": "https://github.com/maha0525/stackchan-mcp/releases/download/<tag>/merged-binary.bin",
            "sha256": "<64 hex>",
            "dest_data": "firmware/merged-binary.bin"
        }
    ]
}
```

`dest_data` の配置先は `~/.saiverse/user_data/addon_data/saiverse-stackchan-addon/firmware/merged-binary.bin` になり、`_firmware_resolve_path()` の解決順序 (3) と一致する。**アドオン側のコードも SAIVerse 本体も変更不要。**

firmware を更新するときは `addon.json` の `url` + `sha256` を差し替えて `setup_version` をインクリメントする。既存ユーザーがアップデートすると installer が step を再実行する (`addon_installer.py:622`)。SHA256 が必須なので、firmware のバージョンは addon.json で pin される形になる = 検証済みの組み合わせだけが配られる。

### C. 誤誘導を消す

- `expansion_data/saiverse-stackchan-addon/firmware/` (自前 firmware の src / dist / platformio.ini) を `archive/firmware/` へ移動する — intent に書かれたまま未実施
- アドオンの `README.md` を v0.4.0 の実態に合わせる (Web Serial 手順は旧世代、現行は esptool + `merged-binary.bin`)
- UI の警告と backend の 404 メッセージに、A+B が失敗したときの入手先 URL を書く (DL 失敗・オフライン環境の受け皿)

### D. 開発者の盲点を塞ぐ (再発防止)

`_firmware_resolve_path()` の (2) `<repo>/temp/stackchan-mcp/firmware/build/` は開発者だけがヒットする経路。`GET /flash/firmware-info` は `source` を返しているので、UI 側で「これは開発者ローカルのビルドです」と明示するか、この経路を環境変数でのみ有効にすると、配布物の欠落が開発中に見えるようになる。

## 裁定が必要な点

1. **fork の Releases に firmware バイナリを置くか** (= まはーが GPL-3.0 の配布者になることを受け入れるか)。置かない場合、「普通に導入して普通に動く」は達成できず、C の案内改善までが上限になる
2. **どのブランチを配布版とするか** — 現状 `integrate/all-fixes-2026-06-24` と `feature/stackchan-imu-readings` が分かれている。配布用の統合ブランチを一本作る必要がある
