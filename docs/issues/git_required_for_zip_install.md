# Issue: ZIP 経由インストールユーザーへの Git 自動導入

**ステータス**: 🟡 進行中 (実装済み・実機テスト待ち)
**優先度**: medium
**作成日**: 2026-05-23
**関連**: `setup.bat`, `setup.sh`, `scripts/install_git_portable.ps1`, `.gitattributes`, `update.bat`, アドオンカタログ管理 (`docs/intent/addon_catalog_management.md`)

## 背景

SAIVerse の運用は Git を前提とした作りになっている:

- **自動更新**: `update.bat` / `update.sh` / `scripts/self_update.py` は `git pull` ベース
- **アドオン取得 (構想中)**: GitHub からのアドオン取得 UI は `git clone` を内部で使う想定
- **開発フィードバック**: 利用者がローカル改変を共有する経路も git 前提

一方で、想定ユーザー層 (大半が Windows) は GitHub の「Download ZIP」経由で入手することが多いと予想される。ZIP 展開後の SAIVerse ディレクトリには `.git/` が無く、Git バイナリ自体もシステムに入っていない可能性が高い。

この状態だと:

- 自動更新が動かない (`update.bat` で git not found)
- アドオン UI が GitHub 取得経路を使えない
- 「Git を別途インストールしてください」と README に書いても、Windows ユーザーには敷居が高い

## 解決案候補

### 案 1: setup スクリプトで Git 自動インストール ✅ 採用

`setup.bat` で git 未検出時に:

1. winget で `Git.Git` を試す (Windows 10/11 標準搭載)
2. 失敗時は PortableGit (GitHub releases の自己展開 7z) を `.git-portable/` に展開し PATH に追加

その後、既存ロジックで `git init` → `git remote add origin` → `git fetch` → `git reset origin/main` を実行し、ZIP 展開済みのディレクトリを追跡対象に昇格する。

CRLF/LF 整合性確保のため `.gitattributes` をリポジトリに追加 (Windows 専用 `.bat`/`.cmd`/`.ps1` は CRLF、その他は LF 統一)。

**メリット**: ユーザーは Git の存在を意識しなくていい。Node.js 自動インストール (Step 2) と同じ流儀で一貫性あり。
**デメリット**: winget が使えない / PortableGit ダウンロードに失敗するエッジケースあり (オフライン環境等)。その場合は手動インストール案内に落ちる。

### 案 2: dulwich でアドオン取得経路だけ吸収

Pure Python 実装の `dulwich` で clone/pull を Python 側で完結させる。SAIVerse 本体は Git 必須にしない。

**メリット**: Git バイナリ不要。
**デメリット**: 本体の `update.bat` も Git 前提なので一貫性が出ない。dulwich は機能制限あり (sparse checkout 等の一部機能サポート薄い)。

### 案 3: README で手動インストール案内のみ

何もしないで手順を README に書く。

**メリット**: 実装コストゼロ。
**デメリット**: 想定ユーザー層のリテラシー的に敷居が高い。

## 採用案の実装内容 (2026-05-23)

| ファイル | 変更 |
|---|---|
| `.gitattributes` (新規) | LF 正規化 + Windows 専用ファイルは CRLF、画像/モデル等は binary |
| `.gitignore` | `.git-portable/` を追加 |
| `scripts/install_git_portable.ps1` (新規) | GitHub releases API で latest PortableGit を取得 → `.git-portable/` に自己展開 |
| `setup.bat` Step 9 | git 未検出時 → winget → PortableGit fallback → 既存の `git init/fetch/reset` フローに合流 |
| `setup.sh` Step 9 | git 無い時に macOS / Ubuntu / Fedora / Arch のインストールコマンドを案内 (自動インストールはしない) |

## 残タスク

- **クリーン環境での実機テスト** (← まはー領域、要クリーン Windows):
  - Windows: Git 未インストール環境で `setup.bat` を実行し、winget 経路 / PortableGit fallback 経路の両方を確認
  - winget が使えない古い Windows での PortableGit fallback 動作確認
  - 既に Git インストール済み環境で再実行しても無害なこと
- **`git reset origin/main` 後の `git status`** が clean になるか確認 (`.gitattributes` の line ending 規則と ZIP 展開時の line ending が衝突しないか)
- ~~update.bat / update.sh / scripts/self_update.py 側で `.git-portable/cmd` を PATH に通す処理が必要か検討 (setup.bat と異なるセッションで起動される場合)~~ → **✅ 対応済 (2026-07-19、下記ログ)**
- README の導入手順を「Git 不要」前提に書き直す

## 関連リソース

- 設計議論: 2026-05-23 のセッション (まはー + エア)
- Node.js 自動インストールの先行実装: `scripts/install_node_portable.ps1`, `setup.bat` Step 2
- PortableGit 配布元: <https://github.com/git-for-windows/git/releases>

## ログ

- 2026-05-23: issue 起票。案 1 を実装 (Windows 自動インストール + .gitattributes 追加)。実機テスト未実施。
- 2026-07-19: **update 経路の PortableGit 断線を修正**。`setup.bat` は自身のセッション内で `.git-portable\cmd` を PATH に前置きするが、`update.bat` / `update.sh` は**別セッション**で起動され、実体の `scripts/update_engine.py` は `shutil.which("git")` と `["git", ...]` を直接叩く。よって「PortableGit しか無い (システム/winget Git 無し)」ユーザーは、後日 update を回すと git 未検出で `assert_git_update_ready` が中断していた。修正: `update_engine._ensure_portable_git_on_path(project_dir)` を新設し `run_update()` 冒頭 (git readiness チェック前) で呼ぶ。`<project>/.git-portable/cmd/git.exe` が在れば同 cmd ディレクトリを PATH 先頭へ前置き (既存なら no-op、非 Windows は `.git-portable` 自体が無いので no-op)。update.bat/update.sh/self_update.py は 3 者とも update_engine.py に委譲するので単一箇所の修正で parity 維持。回帰 `tests/test_update_engine_safety.py` に 3 件追加 (存在時前置き / 不在時 no-op / 冪等)。**setup 経路の自動インストール自体は 2026-05-23 実装のまま。残るはクリーン環境実機テスト (まはー) と README 書き直し。**

## 経緯: ZIP インストールの Git 自動導入 (2026-08-04 in_flight 台帳より移送)

> 台帳の器の再設計 (次アクション欄=前向きのみ) に伴い、それまで台帳セルに積もっていた経緯の全文をここへ移した。時系列の生の堆積であり、整理はしていない。

コードは実装済み(setup.bat の自動 git インストール=winget→PortableGit fallback+git init/fetch/reset / update 経路の PortableGit PATH 通し=2026-07-19 修正 / README を「Git 不要」前提に更新済み)。
残=**クリーン Windows(git 未導入)での実機テスト**: winget 経路・PortableGit fallback・再実行無害・`git reset origin/main` 後の status clean(.gitattributes×ZIP 展開の line ending)。
**次バージョンリリース時にまはーと一緒に実機確認**予定
