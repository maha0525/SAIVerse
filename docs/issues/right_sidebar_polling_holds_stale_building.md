# 右パネルの「現在地」が、部屋を移動しても 10 秒後に前の部屋へ戻る

**ステータス: 未解決 (修正済み・検証待ち)** — 2026-09-21 起票。v0.3.13 を使っているユーザーからの報告。

## 症状

ⓘ (右パネル) を開いたまま別の部屋へ移ると、パネルの「現在地」が一度は新しい部屋になるが、
10 秒ほどで前の部屋の表示に戻る。以後ずっと戻り続けるので、チャットの部屋名とパネルの
部屋名が食い違ったままになる。部屋名だけでなく、インテリア画像・滞在ペルソナ・アイテム
一覧も前の部屋のものが出る。ⓘ を閉じて開き直すと正しくなる。

報告者は、ズレた状態のパネルにある ⚙ (Building 設定) とアイテムの ＋ が、パネルに映って
いる古いほうの部屋に効いてしまう点も指摘している。気づかずにアイテムを追加すると、
別の部屋に入ってしまう。

## 原因

`frontend/src/components/RightSidebar.tsx` の 10 秒ごとのポーリングの useEffect が、
依存配列に `[isOpen]` しか持っていなかった。React の useEffect は依存配列の値が変わった
ときだけ中身を作り直すので、`setInterval` に渡した `fetchDetails` は **パネルを開いた
時点の `currentBuildingId` を掴んだまま** 10 秒おきに呼ばれ続ける。

一つ上の useEffect (部屋が変わったら再取得する側) は `currentBuildingId` を依存に持って
いるので、切り替えの直後だけは正しい部屋が表示される。その後、古い部屋を掴んだままの
ポーリングが上書きする — これが「一度は新しい部屋になり、10 秒後に戻る」の正体。

ESLint はこれを `react-hooks/exhaustive-deps` の警告として出していた。ただし隣の
useEffect に `eslint-disable-next-line react-hooks/exhaustive-deps` が付いており、
React Compiler ベースの react-hooks ルールはこの指示を見るとそのコンポーネント全体の
解析から降りる。つまり **黙らせのコメント 1 個が、このファイルの検査を丸ごと止めていた**。

操作の宛先 (⚙ / ＋ / ペルソナメニュー / 通話) がズレるほうは、別の原因が重なっている。
これらには応答の写しである `details.id` を渡していた。「ユーザーがいま見ている部屋」の
真実は親 (ChatPage) から渡る `currentBuildingId` が持っているので、応答の写しを宛先に
使うと真実が二つになる。

## 直したこと (すべて `frontend/src/components/RightSidebar.tsx`)

1. `fetchDetails` を `useCallback` で `currentBuildingId` に紐付け、ポーリングの依存配列に
   入れた。部屋が変わればポーリングも作り直される。
2. 取得した応答を「どの部屋を要求して得たものか」の印つきで持ち、印が閲覧中の部屋と
   一致するときだけ画面に使う。切り替えてから新しい応答が届くまでの間は「読み込み中」に
   なり、**前の部屋の中身が画面にも操作の宛先にも一瞬たりとも出ない**。
3. 応答が届いた時点で別の部屋へ移っていたら、その応答を捨てる (先に投げた古い部屋の応答が
   後から届いて新しい部屋を上書きする追い越しを防ぐ)。判定に使う ref の同期は、この repo の
   既存の書き方 (`page.tsx` の `currentBuildingIdRef`、`CityMap.tsx` の `scopeRegionIdRef`) に
   合わせた。
4. 操作の宛先を全部 `currentBuildingId` に付け替えた (ItemModal / PersonaMenu / 通話 /
   ItemCreateModal / BuildingSettingsModal)。`details.id` は表示名を取るためだけに使う。
5. 検査を止めていた `eslint-disable-next-line react-hooks/exhaustive-deps` を外した。
   このファイルには react-hooks ルールの黙らせが一つも残っていない。

## 検証 (2026-09-21、Claude が隔離環境で実施)

`test_fixtures/` の隔離環境 (バックエンド 18000 / フロント 18010、合成ペルソナ、自律 OFF、
本番の `~/.saiverse` には未接触) で、報告どおりの手順を踏んだ。二部屋は滞在ペルソナが
違う (Test Lobby = Test Persona A / Test Room A = Test Persona B) ので、ズレると一目で分かる。

- **修正前**: 部屋 A でパネルを開き、部屋 B へ移ると、その後のポーリングが
  `building_id=test_room_a` (前の部屋) を要求し続け、パネルの滞在ペルソナが前の部屋の
  住人に戻った。チャットの見出しは新しい部屋のまま。報告と同じ症状を再現。
- **修正後**: 同じ操作・同じ待ち時間 (16 秒 = ポーリング 1 周以上) で、ポーリングは
  `building_id=test_room_a` (新しい部屋) を要求し、パネルは新しい部屋の内容を保った。
- アイテムの ＋ を開くと「置き場所: Test Room A」= いま見ている部屋になっていた。
- `npx tsc --noEmit` / `npx eslint` / `npm test` (i18n) すべて通過。

**まはーの実機と、報告者の環境での確認が残っている。**

## 三つの原因 (CLAUDE.md「After a failure we caused」)

1. **技術的な原因**: ポーリングの依存配列が `[isOpen]` だけで、変わる値を掴んだまま固まった。
2. **判断の失敗**: 隣の useEffect に付いていた `eslint-disable` を「意図があって正しい」と
   読み、その黙らせがファイル全体の検査を止めることを確かめなかった。黙らせは一行の
   話に見えて、実際には検査の効く範囲を変えていた。
3. **仕組みの条件**: `react-hooks/exhaustive-deps` は警告どまりで `eslint .` は exit 0 を返す。
   警告が出ていても誰も止まらないので、機械が正しく指していた欠陥が人の目に届かなかった。

## 同じ型が他にないか (走査済み)

`frontend/src/` の `setInterval` を全部見た。`CityMap.tsx` と `useClientActions.ts`、
`page.tsx` の三つのポーリングは、変わる値を ref 越しに読む書き方になっていて、この罠を
踏んでいない。RightSidebar だけが例外だった。

`eslint-disable ... exhaustive-deps` が残る他の 5 箇所のうち 4 箇所 (AddonInstallProgressDialog /
MemopediaConversion / CodexLoginModal / WorldEditor) は、依存が変われば作り直される形か
unmount 専用で、この型ではない。**`page.tsx` の `getAddonMetadata` (288 行) だけは同じ型の
疑いがある** — `useCallback(..., [])` で、後から埋まる state を掴んだまま固まっているため、
アドオンのクライアント操作へ常に空のメタデータを渡している可能性がある。別件として
切り出した (アドオン機能の挙動確認が要るので、本件には混ぜない)。

## 歯止めの案 (未実施 — まだ仕組みになっていない)

`react-hooks/exhaustive-deps` を警告から **エラー** へ上げれば、この欠陥も、上に書いた
`getAddonMetadata` の疑いも、将来の同型も機械が止められる。ただし現状 `frontend/src` には
この警告が 29 件あり、上げると `eslint .` が落ちるので、先に既存分を片付ける作業が要る。
**いまの時点では文章の記録しかなく、機械の歯止めは入っていない。**
