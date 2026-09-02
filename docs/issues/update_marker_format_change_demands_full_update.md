# 更新完了マーカーが旧形式だと、書き直すだけで済む状態がフル更新を要求する

**ステータス: 原因確認済み・実装待ち** (2026-09-02 起票。まはーの環境で実際に起動不能を起こした)

## 症状

`start.bat` が毎回「更新が中断されている」と判定し、その続き (`update_engine.py --manual`)
を走らせる。実際には更新など行われておらず、そのチェックアウトは完全に更新済みだった。

```
2026-09-02 14:27:07 [WARNING] Update was interrupted: this checkout was last completed at
                              an unrecorded state but the code is now 0.3.2
```

## 原因

リポジトリ直下の `.update_complete` は、更新が完了した状態を記録するファイル。現在の形式は
JSON (バージョン + `requirements.txt` と `package-lock.json` のハッシュ) だが、それ以前は
**バージョン文字列がそのまま書かれているだけ**だった。

`read_completion_marker` は、この旧形式を読むと JSON パースに失敗して `{}` を返す。
`check_update_complete` はそれを現在の指紋と比較して不一致と見なし、`CHECK_NEEDS_FINISH`
を返す。まはーの環境の中身は `0.3.0.dev6` だった。

**問題は、その「続き」が `run_update` — git pull + 世界スナップショット + pip install +
npm ci のフル更新であること。** 実際に必要なのは「いまの状態を検証して新形式で書き直す」
だけで、コードを引っ張ってくる必要も、世界をまるごとバックアップする必要もない。

コード中のコメントは「旧ビルドのマーカーは一度の finishing pass で新形式に書き直される、
そのコストは `--manual` 一回分」と想定していた。その想定が 76GB の世界の前で崩れた
(スナップショットが制限時間に収まらず、フル更新が完走できない →
`docs/issues/snapshot_timeout_is_fixed_while_world_grows.md`)。二つが噛み合って、
**利用者が自力で抜けられない**状態になった。

## マーカーが「無い」場合は正しく動いている (2026-09-02 実験で確認)

隔離した偽リポジトリで `check_update_complete` の挙動を観測した結果:

| マーカー | 必要な物 | 判定 |
|---|---|---|
| 無し (v0.2 系) | 揃っている | 起動してよい |
| 無し (v0.2 系) | パッケージ不足 | 更新経路へ |
| 無し (v0.2 系) | node_modules 無し | 更新経路へ |
| 旧形式 (ベア文字列) | 揃っている | **更新経路へ** ← 欠陥 |
| 旧形式 (ベア文字列) | パッケージ不足 | 更新経路へ |

マーカーが無いとき、コードは「過去に完走した記録」を探すのをやめ、**現物** —
`frontend/node_modules` があるか、`requirements.txt` の各行が要求どおり入っているか —
を直接見る。だから「コードが古いままでパッケージも古い」は起動可、「コードだけ新しくて
パッケージが古い」は更新経路、と正しく分岐する。

欠陥は 4 行目だけで、**旧形式のマーカーがこの健全な経路に入れないこと**にある。

## 対策 (未実装)

`read_completion_marker` が壊れた内容に対して返す `{}` を、`None` (マーカー無し) と同じ
入り口へ合流させる。判定そのものは変えない。5 行目 (旧形式かつパッケージ不足) は対策後も
更新経路へ行くので、安全側の挙動は落ちない。

## 応急処置 (2026-09-02 実施)

まはーの環境では、`update_engine` 自身の検証関数で「不足パッケージ 0 件・未検査 0 件・
`node_modules` あり」を確認した上で、`write_completion_marker` を呼んで正しい形式へ
書き直した。`--check-complete` は 0 (更新に入らず起動) を返すようになった。

これは 1 台を救っただけで、同じ状態にある他の環境は救われていない。

## 関連

- `scripts/update_engine.py` の `read_completion_marker` / `check_update_complete`
- `docs/issues/snapshot_timeout_is_fixed_while_world_grows.md` — 噛み合ったもう一方
- `docs/issues/v0229_update_bat_truncates_after_git_pull.md` — 過去の更新経路の障害
