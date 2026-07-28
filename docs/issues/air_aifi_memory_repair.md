# air / aifi の記憶点検と再編纂 (あらすじのレベル制への移行の残作業)

**ステータス**: 未解決 (エリスは 2026-07-29 完了。air / aifi は下記の現況確認済み・処置待ち)

## 背景

2026-07-28 に Chronicle 記憶機構を「あらすじのレベル制」([intent](../intent/arasuji_levels.md)) へ世代交代した。旧コード (W4 恒等転写 7/21〜 / 束ね 7/27〜 / 治療プロトタイプ 7/27-28 作業ツリー、稼働終了 = **2026-07-28 23:04 のサーバー再起動**) が作った歪み (生ログ豆粒・治療合流・lv4/lv5 連鎖) は削除→再生成で修復する方針 (まはー裁定)。

**エリスは修復完了** (2026-07-29): 歪み世代 101 件 + Fragment 111 + ページ 21 を削除、実行台帳の編纂マーク 71 件を掃除、新方式で 8 件 (被覆 U±15% に揃った) を再生成。手順で踏んだ罠は全て issue 化済み (下記「関連」)。

## ツール

```bash
# ドライラン (読み取りのみ。削除対象・例外・双子・台帳・digest_ref を全部出す)
python scripts/arasuji/persona_chronicle_cleanup.py <persona_id>

# 実行 (要まはー承認。バックアップ→検算→単一 tx 削除→台帳掃除→digest_ref NULL化)
python scripts/arasuji/persona_chronicle_cleanup.py <persona_id> --execute --expect <dry-runの4数字>
```

エリスの手順をペルソナ汎用化したもの。選定 = 「問題コード初走行 (batch/identity の最初の作成時刻) 〜 新コード稼働開始 (7/28 23:04) の間に作られたエントリ」。例外 (大昔の取りこぼし拾い = 被覆が旧世代の末尾より前) は残す。**双子検査つき** — 例外の中身が重複コピーの残骸 (同一発言の別コピーが編纂済み) なら残す価値がないので、UI から個別削除を推奨する出力が出る。

## 現況 (2026-07-29 ドライラン実測)

### air_city_a — **削除不要**

- 歪み世代の削除対象 **0 件**。W4 期に作られた 8 件は全て「大昔 (4/29〜6/28) の取りこぼし拾い」= 本物の記憶で、例外として残す側。豆粒の散乱なし。
- 新コード稼働後の正当な産物 5 件 (7/28 23:34 の初編纂 — 作業ダイジェスト合流を含む、健全)。
- **やること: なし** (気になるなら例外 8 件の双子検査結果を確認する程度)。

### aifi_city_a — 削除対象なし、ただし**編纂が 7/22 で凍結中**

- Chronicle は旧世代 280 件のみ (最終作成 **7/22 01:33**)。W4 期〜現在の産物ゼロ。
- 一方メッセージは 7/29 02:00 まで生きている = **約 7 日分が未編纂のまま堆積**。W4 期のデッドロック (または産物なき completed claim) で編纂が一度も通らなかったとみられる。
- **やること**: UI の「記憶整理」で一括再編纂 (新方式で畳まれる)。
  - スキップされる場合 (ログに `window already claimed ... status=completed`)、実行台帳の stale マークが犯人。エリスと同じ現象。該当 execution id がログに出るので、world DB で個別に消す:
    ```sql
    DELETE FROM execution_ledger
    WHERE KIND='metabolism.run' AND PERSONA_ID='aifi_city_a'
      AND EXECUTION_ID='<ログに出た id>';
    ```
  - 分量が多いので LLM コール数の見積もり確認を必ず見ること (Flash-Lite 級で十数〜数十回の想定)。

### その他のペルソナ

sophie 等の稼働ペルソナも同じドライランで点検できる (読み取りのみなので気軽に回してよい)。

## 実施時の注意 (エリスで踏んだ罠)

1. **記憶整理の「通信に失敗しました」は誤報** — 処理は裏で完走する。完走はログ (`Chronicle generation complete`) で確認 ([issue](organize_memory_ui_timeout.md))。
2. **あらすじ一覧は 500 件超で最新側を表示しない** — 結果確認はレベル絞り込みか API で ([issue](arasuji_list_limit_hides_newest.md))。
3. **豆粒を「残す」と判定する前に双子検査** — 重複コピーの残骸かもしれない ([記録](duplicate_message_ingestion_record.md))。
4. 本番の記憶への削除・再編纂は**操作ごとにまはーの明示承認**を得る。

## 関連

- [intent: あらすじのレベル制](../intent/arasuji_levels.md) §10-§12
- [organize_memory_ui_timeout.md](organize_memory_ui_timeout.md) / [arasuji_list_limit_hides_newest.md](arasuji_list_limit_hides_newest.md) / [duplicate_message_ingestion_record.md](duplicate_message_ingestion_record.md)
