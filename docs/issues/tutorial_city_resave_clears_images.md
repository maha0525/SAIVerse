# Issue: チュートリアルで City を保存し直すと既存の画像設定が消える

**ステータス**: 🟣 検証待ち（未送信画像を保持する修正は develop に取り込み済み。隔離 UI での再表示の確認は未完了）
**優先度**: high
**作成日**: 2026-10-03
**関連**: [ワールドエディタ設定棚卸し](world_editor_settings_sync.md) / [City の識別子と表示名](../intent/city_identity.md)

## 症状と影響

画像を設定済みの既存 City で TutorialWizard の City 設定保存を通ると、
利用者が画像を編集していなくても `HOST_AVATAR_IMAGE` と `MAP_BACKGROUND_IMAGE` が
`None` になる。現在の画面で利用者が失うのは主に街マップの背景画像の設定参照である。
`HOST_AVATAR_IMAGE` は現在の画面に設定・表示する場所が無い旧項目であり、
案内役のアバターが画面から消えるという実害を確認したものではない。
画像ファイル自体の削除を示す処理は、この更新経路にはない。

## 原因の経路（調査版 `53944ed9faf3eecd03e92ea5a07ec1b1329479a2`）

1. [`TutorialWizard.saveCityName`](https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/tutorial/TutorialWizard.tsx#L296-L323)
   は既存 City を取得し、表示名・説明・オンライン設定・ポート・タイムゾーン・言語を PUT する。
   **`host_avatar_path` と `map_background_image` は送らない**。
2. [`CityUpdate` と PUT の受け渡し](https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/api/routes/world.py#L42-L52)
   は未送信の2フィールドを `None` にし、[更新ルート](https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/api/routes/world.py#L205-L207)
   がその値を manager へ渡す。未送信と明示的な解除が区別されない。
3. [`AdminService.update_city`](https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/manager/admin.py#L290-L303)
   は `HOST_AVATAR_IMAGE` と `MAP_BACKGROUND_IMAGE` をともに `None` で上書きして commit する。

## 修正を選ぶときの条件（起票時）

画面の入力 → 更新 API → DB → 再表示までで、**編集していない設定は保持し、明示した解除だけを反映する**。

- 送信側で既存の画像値を再送する案: 現在の全項目 PUT 契約を保つ小さい修正。
  ただし取得後の別画面の変更を古い値で戻す窓は残るので、他の全項目保存経路も確認する。
- 更新 API で未送信項目を保持する案: 保存の持ち主で全 caller に共通の区別を与える。
  明示的な `null` / 空文字による解除と未送信を分け、既存 caller の互換を確かめる必要がある。

起票時点では方式を決定せず、ランタイムも変更していなかった。City の内部識別子の不変条件は維持する。

## 採用した修正

- `CityUpdate` の画像欄は `Optional[str]` のまま保ち、PUT ルートは
  `model_fields_set` で未送信を区別する。JSON / OpenAPI に内部 sentinel を出さない。
- `SAIVerseManager.update_city` → `AdminService.update_city` は既存の `UNSET` を
  受け渡す。未送信の画像列は DB に代入せず、`null` / 空文字 / 空白だけの文字列を
  明示したときは従来どおり `NULL` にする。片方だけの更新はもう片方を保持する。
- 旧ホスト画像のアップロード引数は引き続き更新に使える。画像未送信なら
  `reload_host_avatar` も呼ばず、既存のメモリ上の画像設定に触れない。
- TutorialWizard に古い画像値を再送させないため、取得後に CityMap の PATCH が
  書いた新しい背景を、画像を扱わない再保存が巻き戻さない。WorldEditor も
  `host_avatar_path` を送らないので、同じ保存側の修正で旧列を保持する。

画像以外の必須項目・作成 payload・識別子の不変性は変更しない。画像ファイルは
削除しない。省略した通常の Python caller も保持、明示した `None` は解除となる。

## 検証の出口

隔離 DB の合成 City に2画像を設定し、チュートリアルと同じ payload → 実 API / manager → DB の再読込まで通す。
画像未送信時の保持、明示解除、片方だけ変更、初回の画像なし City を確認する。
さらに隔離 UI で再保存 → CityMap の背景再表示を確認する。旧 `HOST_AVATAR_IMAGE` は DB の保持契約を調べ、存在しない現行 UI の再表示を検証条件にしない。本番 City は使わない。

### 済んだ検証と残る境界

- `tests/test_city_image_preservation.py` の20件で、POST による合成 City 作成 →
  初回画像 PUT → チュートリアルと同形の再保存 → 新しい DB セッション →
  `/api/db/tables/city` と `/api/info/city-map` の読み戻しまで通した。
- 2回連続の省略保存、初回画像なし、片方変更、両方または片方の明示解除、取得後に
  別の PATCH が更新した背景の保持、manager / AdminService 直接呼出しの省略、
  旧アップロード経路、文字列 / null の入力検証・OpenAPI の型互換を固定した。
  旧アップロード処理自体と実画像ファイルの読込は fake であり、その I/O は未検証。
- 変更前はチュートリアル再保存で `HOST_AVATAR_IMAGE` が `None` になる失敗を再現。
  変更後は新設20件と既存 `tests/test_city_identity.py` 22件が通過し、変更 Python の
  `ruff check` も通過した。
- 周辺の DB API、AI 編集、Building ID、world audit、アイテム表示上限も含む
  7ファイルの回帰検査は139件と13 subtests が通過した。
- 参照ドキュメントを再生成し、API と DB schema は差分なし。tool catalog はこの
  隔離 checkout に未導入のアドオン50件が落ちる環境差分のみだったため保持した。
  そのため `gen_reference_docs.py --check` は tool catalog の既存差分だけで失敗する。
- 未検証: 実ブラウザの TutorialWizard 操作 → CityMap 再表示、まはーの実機確認。
  API 応答に背景が残ることを、ブラウザの画像描画成功や実機確認と同一視しない。

- 既存の残る境界: WorldEditor で City を選ぶと、その時点の `MAP_BACKGROUND_IMAGE` が
  `formData.map_background_image` に入り、画像欄を編集していなくても保存のたびに送られる。
  そのフォームを開いたまま別画面の CityMap で背景を変更し、続いて WorldEditor で
  名前など別の項目を保存すると、新しい背景をフォームの古い値で上書きする。
  これはこの PR より前からある同一 City の並行編集の問題で、未送信を保持する修正では
  防げない。WorldEditor は City 一覧を開くたびに読み直すため、通常の開き直しとは区別する。
  将来直すなら「画像欄を編集していない保存では、その画像欄を送らない」形にする。
  今回はこの境界の記録だけとし、WorldEditor の送信処理は変更しない。

## 記録

- 2026-10-03: [PR #362 のレビュー](https://github.com/maha0525/SAIVerse/pull/362#issuecomment-5964788305) の指摘を、上記3境界のソースで確認して起票。これはソース照合の記録であり、この文書追加で修正・実機検証が完了したとは扱わない。

- 2026-10-03: [再レビューの補足](https://github.com/maha0525/SAIVerse/pull/362#issuecomment-5966303464) と、現行 TutorialWizard / WorldEditor / CityMap の画像フィールドを照合し、DB の2列の変更と、現在の画面で見える地図背景の実害を区別した。ランタイム変更・実機操作は行っていない。

- 2026-10-03: 保存側で未送信を保持する方式を実装。上記の隔離 API 往復と既存識別子の回帰検査を実施した。状態は未着手から検証待ちへ進め、UI / 実機の残りを台帳へ追加。本番 City・ペルソナ・LLM は使用していない。

- 2026-10-04: [PR #368 のレビュー](https://github.com/maha0525/SAIVerse/pull/368#issuecomment-5975901727) を受け、WorldEditor の古い画像値の再送による並行編集の境界と将来の修正方針を記録した。コードの変更・追加の実機検証は行っていない。

- 2026-10-04: PR #368 を develop へマージした (レビューはメティス、マージの判断はまはー)。台帳に「検証待ち・私 (隔離環境の画面確認)」として追加した。
