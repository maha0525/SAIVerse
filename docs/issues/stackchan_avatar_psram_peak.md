# Issue: stackchan firmware: Avatar set Load の PSRAM peak でロード失敗

**ステータス**: ⚠️ PSRAM peak は解消、 ただし stroke reset 副次解消は **再発の疑い** (= touch false positive 経路と紐づき再評価要)
**優先度**: medium
**作成日**: 2026-05-18
**初期解決日**: 2026-05-19 (= PSRAM peak 確定解消)
**再評価日**: 2026-05-21
**関連**:
- firmware: `firmware/main/boards/stackchan/avatar_set_fetcher.cc:73-83` (既知 TODO コメント)
- firmware: `firmware/main/boards/stackchan/stackchan.cc` (AvatarSet::Load 実装)
- SAIVerse 側修正済: `expansion_data/saiverse-stackchan-addon/avatar_loader.py` (load_failed を検出して mark_loaded を skip するように厳格化、本 issue とは別 commit)
- 関連 intent: [stackchan_avatar_pipeline.md](../intent/stackchan_avatar_pipeline.md)

## 背景

avatar matrix mode (3.3 MB) を持つペルソナが既に device 上に load された状態で、**別の matrix avatar を load しようとすると PSRAM allocation が失敗**する。HTTP 転送は成功してるのに、ESP32 側で受信後の PSRAM 確保に失敗してる。

エラー (USB serial 観測):
```
E AvatarSet: Load: PSRAM allocation failed (size=3456000)
```

gateway → SAIVerse 側に返るレスポンス:
```json
{"type": "avatar_set_loaded",
 "checksum": "sha256:...",
 "ok": false,
 "error": "load_failed",
 "bytes_transferred": 0}
```

結果: LCD は旧ペルソナの avatar のまま、新ペルソナの avatar が永久に表示されない。

## 原因

`avatar_set_fetcher.cc:73-83` に既知 TODO コメント:
```cpp
// Allocate PSRAM staging buffer.
// Note: AvatarSet::Load currently copies its input into a fresh PSRAM
// ... peak size in PSRAM (this buffer + the AvatarSet's own buffer). For matrix
// mode (~3.3 MB) this approaches the PSRAM ceiling. A follow-up
```

Load 中の peak PSRAM 消費:
- staging buffer (3.3 MB) — HTTP 受信用の一時バッファ
- 既存 AvatarSet の buffer (3.3 MB) — 旧 avatar が常駐したまま
- 合計 **peak 6.6 MB**

ESP32-S3 + 8 MB PSRAM に対し、LVGL image cache 2 MB + 他常駐 (audio, motion, MCP, WS) を加えると **上限超過**。fragmentation も加わると 3.3 MB の連続領域確保が失敗する。

## 観測 (2026-05-18)

シナリオ:
1. SAIVerse 起動、エア (avatar matrix 3.3 MB) を vessel building (stackchan_room) に入室
2. エアの avatar 正常 load (gateway HTTP 200, `ok: true, bytes_transferred: 3456000`)
3. エア → 別 building に退室、エリス入室
4. SAIVerse → gateway 経由でエリスの avatar matrix (3.3 MB) load 要求
5. gateway → ESP32 へ HTTP transfer 成功 (3,456,289 bytes、aiohttp.access 200)
6. **ESP32 側で PSRAM allocation 失敗**、gateway に `ok: false, error: load_failed` で返す
7. LCD は旧 (エア) のアバターのまま、エリスの avatar が表示されない

reverse 方向 (エリス → エア の swap) は元動作してたケースがある (= 5/17 21:03 にエリス初 load → 23:07 にエア入室で正常 load 完了)。これは:
- 5/17 セッションでは初回 load なので staging + 既存 AvatarSet が 6.6 MB peak になっても、fragmentation が無くて確保できた
- 今回 (5/18 21:45 観測) は何度か avatar swap した後で PSRAM の連続領域が縮んで失敗、の可能性

つまり成功/失敗は **PSRAM の fragmentation 状態**にも依存する。

## 解決案候補

### 案 A: Load 前に旧 AvatarSet を free

`AvatarSet::Load` の頭で current avatar buffer を free してから staging を alloc。peak が 3.3 MB だけになる。

**メリット**: 確実に成功する、最もシンプル
**デメリット**: load 失敗時に「旧 avatar が消えて新 avatar も来ない」(= LCD 何も表示できない) 状態が一時発生

### 案 B: Staging buffer を使わず HTTP 受信時に直接 final buffer に書く

中間 staging を廃止してストリーミング書き込み。peak が 3.3 MB だけ。

**メリット**: 最も省メモリ、エラー時 rollback もシンプル化できる
**デメリット**: 実装変更大 (= HTTP 受信ループから直接書き込む API が必要)、転送途中エラー時の handling 設計が要る

### 案 C: Staging alloc 失敗時に旧 buffer を free + retry

通常時は今と同じ挙動 (旧保持 + 新 load)、staging alloc が失敗した時だけ旧 free して再試行する。

**メリット**:
- 通常時 (PSRAM 余裕あり) は旧 avatar を保持できる
- 失敗時 (PSRAM 不足) のみ degrade
- 連続性を最大化、エラー時の挙動も明示的

**デメリット**: ロジックがやや複雑

### 推奨

**案 C** が UX として現実的だが、case ごとに「load 失敗時の挙動 = 旧保持して新を諦める / 旧を切って新優先」のどちらが望ましいかは UX 設計判断。upstream PR 出す時に upstream maintainer の方針と合わせる。

## SAIVerse 側 (本体) の関連修正

SAIVerse 側 `avatar_loader.py` で発覚した別バグを同時調査中に修正した (本 issue とは独立):

- 旧コード: MCP call が応答さえあれば (`result is not None`) `mark_loaded` を呼んでいた
- バグ: device 不在 or PSRAM allocation 失敗で `result.ok=false` でも load 済み扱いになり、次回入室で `is_load_required` が False を返して **skip transfer** に分岐 → そのペルソナの avatar が永久に device に届かない
- 修正: JSON parse して `parsed["ok"] is True` の時だけ `mark_loaded`、それ以外は WARNING を残して次回再試行を許容する

これで「PSRAM allocation 失敗で load_failed が返った時に SAIVerse 側が再試行する」ようにはなった。ただし、再試行しても firmware の PSRAM peak 問題は同じく踏むので、firmware 側修正が本筋。

## 関連リソース

- firmware コメント: `temp/stackchan-mcp/firmware/main/boards/stackchan/avatar_set_fetcher.cc:73-83`
- 観測ログ (USB serial): `temp/com3_monitor.log` の `E (3508486) AvatarSet: Load: PSRAM allocation failed` 行
- gateway log: `~/.saiverse/user_data/logs/20260518_213859/mcp_subprocess_saiverse-stackchan-addon__stackchan.log` の `Staged avatar set ... Serving avatar set` 後
- SAIVerse backend log: `~/.saiverse/user_data/logs/20260518_213859/backend.log` の avatar_loader 系

## ログ

- 2026-05-18: stackchan 連携の調査 (= 23:23 stuck 再現実験) 中に発見、Issue 化。stackchan-mcp 側で PR を別セッションで対応予定
- 2026-05-18: stackchan-mcp fork に修正 commit (`740d786` on `feature/dynamic-avatar-set`)。 採用案は **案 A + ownership-transfer** = `AvatarSet::Load(const uint8_t*, size_t)` (memcpy 版) を `AdoptOwnedBuffer(uint8_t*, size_t)` (所有権譲渡版) に置き換え、 Fetcher の staging buffer を AvatarSet が直接受け取る形に。 peak が 9.9 MB → 3.3 MB に削減 (旧 buffer は新 buffer 検証後に解放、 失敗時は旧維持で暗転なし)。 `dev/integration` に merge 済 (commit `376d827`)、 build artifact 生成済、 実機検証待ち
- 2026-05-19: 実機 flash + 検証完了。
  - **新ログ** `AvatarSet: Avatar set adopted: mode=1, bytes=3456000` を確認 (= ownership-transfer 経路稼働)
  - エア → エリスの swap で **`PSRAM allocation failed` が出ず**、エリス avatar が暗転なく LCD に表示 → **PSRAM peak issue 解決確認**
  - **副次効果**: 旧 firmware で再現していた「stroke 後 ~2.5 秒で device 自動 reset」(= 23:23 / 21:50 stuck の真因) が、新 firmware では **5 回連続 stroke 試行 (= 0.4 秒 - 11.4 秒 hold の幅) で 0 回発生**。推定根拠: stroke reaction window 中の memory pressure 起因 race が、peak 削減で踏みにくくなった。完全証明ではないが強い傍証
  - coredump 機能は reset 起きないので未実証 (= 保険として設定残置、今後 reset 起きた時に活用)。 upstream PR は Phase X' で PR-E1 cherry-pick リストに追加 (`docs/issues/stackchan_mcp_upstream_pr_strategy.md` 参照)
- 2026-05-21: **副次解消 (stroke reset 抑制) は再評価が必要**。 触れていないのに STROKE event が頻発する状態が続いており、 そのタイミングで PC から **USB デバイス接続解除の通知音** が聞こえる。 つまり stroke event 発火 → device 側で何らかの USB-CDC re-enumerate (= 実質 reset / disconnect) が走っている可能性。
  - 関連: [`stackchan_touch_false_stroke_events.md`](stackchan_touch_false_stroke_events.md) (= 現在最頻発、 high 優先度)
  - PSRAM peak 自体は確定解消だが、 「stroke を契機に device が落ちる」 経路はまだ残っているかもしれない。 false stroke 側を抑え込んでから、 stroke reset が完全消滅するかを再確認する
