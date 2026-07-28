# air / aifi の記憶点検と再編纂 (あらすじのレベル制への移行の残作業)

**ステータス**: ほぼ完了 (エリス = 2026-07-29 完了。air = 削除実施済み・再編纂待ち。aifi = 削除対象なし・再編纂のみ)

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

### air_city_a — **W4 期産 8 件の削除実施済み (2026-07-29)。残り = 再編纂**

- まはー裁定 (2026-07-29): 旧コードで編纂されたものは内容が本物でも新版で回し直す。→ スクリプトに `--strict-window` (例外温存をやめ窓内全削除) を追加して実施。
- **実施結果**: エントリ 8 + Fragment 19 + ページ 3 を単一 tx で削除 (Chronicle 219→211、旧世代 206 無傷)。実行台帳マーク 1 件削除。バックアップ `memory.db.bak-cleanup-20260729-031512` (210MB)。
- **残り = 再編纂**: 未処理 72 通 → LLM 13 コール (flash-lite、`build_arasuji.py --estimate` 実測)。UI の記憶整理でも `build_arasuji.py` でも可。
- ⚠️ **air にも二重取り込みが 8 組** (11/25×1、6/17-18×4、7/18-19×3)。うち 7/18-19 の 3 組は削除した 459d5226 の被覆内で、**再編纂の前に余剰コピー (各組の後の行 = rowid 3715/3716/3718) を消さないと、両コピーとも新あらすじの材料に入る**。11/25・6/17-18 の 5 組は温存した旧世代エントリの中に両コピーとも編纂済み (エリスの 4 組と同じ形 = 片方削除は安全だが急がない)。
- 新コード稼働後の正当な産物 5 件 (7/28 23:34) は無傷で温存。

### aifi_city_a — **削除対象なし (実測確定)。凍結の実害は当初見立てより小さい**

- 7/22 02:59 以降に作成された Chronicle は **0 件** (最終作成 7/22 01:33、問題コード産の batch/identity 自体が無い)。削除する物が存在しない。
- 本 issue の旧版は「約 7 日分が未編纂のまま堆積」と書いたが、これは生メッセージの存在期間だけを見た数字だった。**編纂対象の未処理は 7 通のみ** (`build_arasuji.py --estimate` 実測、LLM 4 コール) — 7/22 以降のメッセージの大半は編纂対象外の種別だった可能性が高い (未検証)。
- **やること**: UI の「記憶整理」で再編纂 (4 コール)。スキップされる場合 (ログに `window already claimed ... status=completed`)、実行台帳の stale マークが犯人。該当 execution id がログに出るので、world DB で個別に消す:
    ```sql
    DELETE FROM execution_ledger
    WHERE KIND='metabolism.run' AND PERSONA_ID='aifi_city_a'
      AND EXECUTION_ID='<ログに出た id>';
    ```

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
