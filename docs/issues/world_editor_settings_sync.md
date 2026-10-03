# Issue: ワールドエディタが他経路の設定追加に追いついていない(同期監査)

**ステータス**: 🟣 検証待ち (City / Building / Persona の机上棚卸し済み。まはーの範囲・優先順位判断待ち。UI 追加は未着手)
**優先度**: mid
**作成日**: 2026-07-08
**関連**: `frontend/src/components/settings/WorldEditor.tsx` ↔ 各設定モーダル(`GlobalSettingsModal.tsx`, `BuildingSettingsModal.tsx`, `SettingsModal.tsx` 等。旧 `PersonaProfileModal.tsx` は削除済み)

## 背景

City / Building / Persona などの設定項目が、個別の設定モーダルやチャット経路で色々**足されてきた**のに、俯瞰編集する側の **ワールドエディタ (`WorldEditor.tsx`) が追いついていない**。本来はワールドエディタと個別経路で同じ項目を編集できるべき(同期しているべき)。

## 調査事項(棚卸しが主体)

1. `WorldEditor.tsx` が現在編集できる項目を、対象(City / Building / Persona / …)ごとに列挙する。
2. 各個別経路(`GlobalSettingsModal` / `BuildingSettingsModal` / `SettingsModal` / 作成ウィザード等)が編集できる項目を列挙する。
3. **差分表**を作る = 「個別経路にはあるがワールドエディタに無い項目」を洗い出す。これが埋めるべき穴。

## 2026-10-02 棚卸し — City / Building / Persona

### 対象と読み方

- 調査基準は `53944ed9faf3eecd03e92ea5a07ec1b1329479a2`。以下のソースリンクはこの版に固定した。入力欄だけでなく、送信 payload と受け側も照合した。実機操作・本番データ変更・LLM 実行はしていない。
- **既存データの編集**と**新規作成**を分ける。「UI に無い」と「保存先 API に無い」も区別する。差分は追加候補であって、全項目の追加決定ではない。
- 個別ペルソナ設定の現行入口は `SettingsModal`。旧 `PersonaProfileModal` は [Track 撤廃時に削除済み](../intent/track_retirement.md)。削除済み UI の項目を復活候補に数えない。
- 今回の差分比較は City / Building / Persona と、その作成・街マップ経路に限定する。WorldEditor の Blueprint / Tool / Item / Playbook タブ、RSS 設置物、アドオン固有設定は全項目比較していない。

### 1. 現在編集できる項目

| 対象・経路 | 保存できる項目 | 制限・根拠 |
|---|---|---|
| City / WorldEditor | 表示名 `name`、説明 `description`、`ui_port`、`api_port`、`timezone`、`language`。既存 City は背景 `map_background_image`、起動時オンライン設定 `online_mode` も編集 | 内部識別子 `slug` は作成時だけ。既存では表示のみ。[WE-C] / [W-API-C] |
| City / TutorialWizard | 表示名、タイムゾーン、言語 | 説明・オンライン設定・ポートは既存値を再送する。`host_avatar_path` / `map_background_image` は未送信で、保存時に既存の画像設定を `None` へ消してしまう（[未解決 issue](tutorial_city_resave_clears_images.md)）。[T-C] / [W-API-C] / [W-C-SAVE] |
| City / CityMap | 表示名、背景画像の設定・解除 | それぞれ専用 PATCH。[MAP-C] |
| Building / WorldEditor (既存) | 名前、説明、収容数、システム指示、内部画像、追加プロンプトファイル、アイテム表示上限、旧 Tool 紐付け、旧自動インターバル | Building ID と所属 City は表示のみ。[WE-B] / [W-API-B] |
| Building / BuildingSettingsModal | 上記の既存 Building 項目に加え、事前実行 Spell の追加・削除 (Spell 名、引数、ラベル) | City 選択 UI はあるが変更はサーバーに拒否される。Spell は通常の保存ボタンと独立した即時 POST/DELETE。[B-FORM] / [B-SPELL] |
| Building / WorldEditor (作成) | 名前、任意の Building ID、City、収容数、説明、システム指示 | 作成 payload はこの 6 項目。調査時点の自動インターバル欄は表示されるが POST には含まれない。[WE-B-SAVE] |
| Building / Sidebar (作成) | 名前 | 現在の City を使用。収容数 10、説明・指示は空で作成する簡易入口。[SIDEBAR-B] |
| Building / CityMap | マップ座標 `MAP_X` / `MAP_Y` | マップ編集の位置保存。通常の Building 設定とは別 API。サーバー側の City スコープ検証なし: `building_id` だけで行を検索する。[MAP-UI] / [MAP-B] |
| Persona / WorldEditor (既存) | 名前、説明、システムプロンプト、ホーム City、標準モデル、軽量モデル、自律 ON/OFF、アバター、外見画像 | 建物への移動は別操作。自律 UI の残存には下記注意。[WE-P] / [WE-P-SAVE] |
| Persona / SettingsModal | 説明、システムプロンプト、標準・軽量モデル、アバター、外見画像に加え、次表の個別設定 | 名前は表示のみ、ホーム City の変更欄なし。事前実行 Spell は別の即時保存。[P-IDENTITY] / [P-SAVE] / [P-BASIC] |
| Persona / WorldEditor (作成) | 名前、システムプロンプト、ホーム City | 説明欄も見えるが作成 payload に含まれない。既存編集との相違。[WE-P] / [WE-P-SAVE] |
| Persona / PersonaWizard (作成) | 名前、システムプロンプト、ホーム City、任意のカスタム ID | City 選択は複数 City 時のみ。ID 無指定時はバックエンドで生成。[P-WIZARD] |

### 2. 個別経路にあり WorldEditor に無い項目

| 対象 | 項目 | 個別経路・ソース | WorldEditor 側の状態 / 判断前の区別 |
|---|---|---|---|
| Persona | 言語 `language` (City を継承 / ja / en) | SettingsModal [P-LANGUAGE] | UI なし。`AIUpdate` と更新経路は受理済み。[W-API-P] |
| Persona | `memory_weave_model`、`vision_model`、`audio_model`、`video_model`、`reflex_judgment_model` | SettingsModal [P-MODELS] | UI と `AIUpdate` に無し。非表示モデルは未送信時に保持する契約が既にある。追加時も壊さない。[W-P-SAVE] |
| Persona | `chronicle_enabled`、`autonomous_chronicle_enabled`、`auto_recall_enabled`、`auto_recall_enhanced`、`memopedia_index_enabled` | SettingsModal [P-MEMORY] | UI なし。`AIUpdate` と更新経路は受理済み。[W-API-P] |
| Persona | `memory_weave_context`、`core_memory_char_budget`、`chronicle_char_budget` | SettingsModal [P-MEMORY] | UI と `AIUpdate` に無し。個別 PATCH は送信する。[P-SAVE] |
| Persona | `spell_enabled` | SettingsModal [P-SPELL] | UI なし。`AIUpdate` と更新経路は受理済み。[W-API-P] |
| Persona | `realtime_current_time_enabled`、`realtime_last_utterance_enabled` | SettingsModal [P-SPELL] | UI と `AIUpdate` に無し。[P-SAVE] |
| Persona | `meta_judgment_config.keep_cache_alive`、同 `cache_threshold_ratio` | SettingsModal [P-CACHE] | UI と `AIUpdate` に無し。比率欄は保温 OFF 時に無効。ほかの旧 Meta 設定キーは表示・編集項目に数えない |
| Persona | `user_conv_timeout_minutes` | SettingsModal [P-CACHE] | UI と `AIUpdate` に無し。[P-SAVE] |
| Persona | 紐付けユーザー `linked_user_id` | SettingsModal [P-BASIC] | UI と `AIUpdate` に無し。個別 PATCH が `UserAiLink` を更新する別の関連データ。[P-LINK] |
| Persona / Building | 事前実行 Spell の追加・削除、作成時の `spell_name` / `spell_args_json` / `label` | SettingsModal [P-RT-SPELL] / BuildingSettingsModal [B-SPELL] | WorldEditor に管理 UI なし。専用 API の機能であり、通常の設定 payload に足すだけでは揃わない。応答の `enabled` / `priority` は両モーダルとも編集欄が無いため差分に数えない |
| Persona (作成のみ) | カスタム `ai_id` | PersonaWizard [P-WIZARD] | WorldEditor に欄なし。共通の `AICreate` は受理済み。[W-API-P] |
| Building | マップ座標 `MAP_X` / `MAP_Y` | CityMap [MAP-B] | WorldEditor に数値編集欄なし。サーバー側の City スコープ検証が無い既存の制限と、専用経路を重複させるかの判断を分ける |

**この範囲で City の個別経路だけにある設定は見つからなかった。** チュートリアルの表示名・言語・タイムゾーンと、街マップの背景画像は WorldEditor にもある。`host_avatar_path` のような API 欄だけを根拠に「個別 UI の追加に追いついていない」とは数えない。

### 3. 揃えてはいけない制限・既存の別件

- **City の識別子**: 既存 `CITY_SLUG` は変更不可、表示名だけ変更可。説明をチュートリアルで聞かないのも明示的な方針 ([city_identity.md §3–4・§8](../intent/city_identity.md))。全経路を同じフォームにする理由にはしない。
- **Building の所属 City**: WorldEditor の無効化は契約どおり。個別モーダルの選択可能 UI がサーバーの拒否と食い違う ([B-FORM] / [B-CITY-GUARD])。WorldEditor を有効化して揃える修正は不可。
- **Persona の名前・ホーム City**: 個別 PATCH は現在値を保持し、名前変更を意図的に扱わない ([P-CONTRACT])。WorldEditor の方が編集範囲が広い。移動は別操作であり設定欄に混ぜない。
- **自律 ON/OFF**: WorldEditor には残るが、SettingsModal は値を保持するだけで UI は非表示 ([WE-P] / [P-AUTONOMY])。[v0.3 は運転 UI を隠す方針](../intent/autonomous_behavior_v3.md) のため、個別画面へ再追加して揃えてはいけない。WorldEditor 側の残存は方針との整合確認事項。
- **旧自動インターバル**: 調査版では両方に入力欄がある ([WE-B] 738 行 / [B-FORM] 314–322 行)。新機能の欠落ではなく [削除 issue](building_auto_interval_setting_removal.md) の対象。別 draft 作業 `feature/remove-obsolete-building-interval-inputs` で両入力欄を撤去予定 (保存互換は保持)。この監査は削除・DB 廃止の判断を行わない。
- **アイテム表示上限**: 両 UI に実装済み。空欄 = 既定、0 = 非表示を同じように扱う ([WE-B] / [B-FORM])。[両経路への配置は裁定済み](../intent/room_item_display_cap.md) なので欠落に数えない。
- **旧 Tool 紐付け**: 両 UI に欄があるが、`BuildingToolLink` は現在の Tool/Spell 実行経路では使われない ([CLAUDE.md](../../CLAUDE.md))。新しい Spell 管理と同一視しない。
- **チュートリアルの City 再保存**: 未編集の案内役アバターと地図背景が消える。送信 payload → `CityUpdate` の未送信時の既定値 → `AdminService.update_city` の上書きまで照合した。画面の不足とは別の保存不具合として [issue](tutorial_city_resave_clears_images.md) に分離し、今回ランタイムは変更しない。
- **CityMap の座標保存**: `PUT /api/world/buildings/positions` は Building ID ごとに更新し、対象が現在の City に属するかをサーバーで検査しない。画面が現在の街の建物を送ることと、API が所属を保証することは別。所属 City 自体を変更する API ではない。
- **モデル欄を消す過去の不具合**: [別 issue で修正済み](archive/world_editor_save_wipes_persona_model_overrides.md)。追加モデルの編集欄が無いことと、保存で既存値を消すことは別件。[現行保存処理][W-P-SAVE]は未送信欄を保持する。
- **作成フォームの表示と送信**: Persona の説明欄は新規作成でも表示されるが、POST は送らない ([WE-P] 862 行 / [WE-P-SAVE] 540 行)。これは優先順位の判断とは別に、追加前に確認できる既存の不整合。今回の docs 変更では直していない。

### 4. GlobalSettingsModal / チャット設定との境界

`GlobalSettingsModal` の world タブは [WorldEditor 自体を表示する][G-WORLD]。ほかのタブが扱う次の設定は、City / Building / Persona の同じレコード欄の欠落とは分ける。

| 設定・操作 | 現在の持ち主・根拠 | この棚卸しでの扱い |
|---|---|---|
| 画面の言語、テーマ | `LocaleControls` とブラウザのテーマ設定 [G-LOCAL] | City の言語や Persona の言語とは別の利用者 UI 設定 |
| 環境変数、全体のモデルロールとプリセット、モデル/プロバイダ定義 | `/api/admin/env`、チュートリアルのモデル API、モデル管理パネル [G-ENV] / [G-MODELS] / [G-PANELS] | 個別 Persona のモデル上書きとの差分表に混ぜない |
| 開発者モード、更新確認、告知監視、配布チャンネル | 全体設定と更新操作 [G-SYSTEM] | 特定 City の設定欄として追加する根拠は未確定 |
| 画像品質、メディア想起、反射判断タイムアウト、Gemini 自動キャッシュと保持秒数 | `/api/config/*` [G-MEDIA] | 全体設定。ペルソナ個別欄とは所有範囲が異なる |
| 整理後に残す量 / 整理を始める量 `metabolism_target_chars` / `metabolism_high_chars` | 全体既定 [G-WATERMARK] | `core_memory_char_budget` / `chronicle_char_budget` とは別の設定 |
| Playbook 実行権限、RSS 設置物/購読、説明補完などの保守操作 | 権限・RSS・utilities タブ [G-OTHER] | 別対象または操作。City / Building / Persona の同一欄比較は未実施 |
| チャットの一時モデル上書き、パラメータ、画像埋込上限 | `ChatOptions` [CHAT] | 永続 Persona モデル設定と同一視しない。優先順は [persona_model_selection.md](../intent/persona_model_selection.md) が正典 |

### 5. 次に決めることと未検証の境界

1. まはーと §2 の各項目を、WorldEditor に置くもの / 個別画面への導線で足りるもの / 専用画面に任せるものへ分け、順番を決める。**この監査は追加範囲・優先順位・共通フォーム化を決定しない。**
2. UI 追加を選んだ項目だけ、未送信・解除・既定値の継承・同時編集を含む保存契約を設計する。通常保存と Spell の即時保存は同じものとして扱わない。
3. 実装した場合は隔離環境で、個別画面 → 保存 → WorldEditor → 再表示とその逆、画面の扱わない値の保持、別対象への切替・閉じる/再表示を確認する。本棚卸しはソース照合であり、この往復や実機見た目を検証したものではない。

## 解決案候補

- 差分の項目をワールドエディタに追加していく(短期)。
- 中長期: 設定項目の**定義を単一ソース化**し、ワールドエディタと個別モーダルが同じ定義から UI を生成する仕組みにして、今後のドリフトを構造的に防ぐ(要設計判断。やりすぎない範囲で)。

## 注意

- どこまでワールドエディタに集約するかは情報設計の判断を含むので、差分表を出した上で**まはーと優先順位**を決める(全項目を機械的に足すのが正解とは限らない)。

## 関連リソース

- `frontend/src/components/settings/WorldEditor.tsx`
- `frontend/src/components/GlobalSettingsModal.tsx` / `BuildingSettingsModal.tsx` / `SettingsModal.tsx`
- アイディア帳: `docs/overview/ideas.md`「UI / プラットフォーム」

## ログ

- 2026-07-08: 起票。ideas.md から昇格。まず差分表の作成から着手する。

- 2026-10-02: City / Building / Persona の入力・送信・受け側を机上照合し、差分表と意図的な制限を追記。UI 追加・DB 変更・本番操作は行っていない。追加範囲と優先順位は未決のまま。

- 2026-10-03: [PR #362 のレビュー](https://github.com/maha0525/SAIVerse/pull/362#issuecomment-5964788305) を再照合し、TutorialWizard の画像保持という誤記を訂正、未解決 issue を分離。CityMap の座標更新に所属検査が無いことを追記。実機の保存往復は引き続き未検証。

<!-- 棚卸し時点のソース。後続の UI 整理で行が動いても参照先を保つ。 -->
[WE-C]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/settings/WorldEditor.tsx#L681-L711
[W-API-C]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/api/routes/world.py#L31-L52
[T-C]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/tutorial/TutorialWizard.tsx#L296-L323
[MAP-C]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/CityMap.tsx#L534-L628
[WE-B]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/settings/WorldEditor.tsx#L725-L799
[W-API-B]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/api/routes/world.py#L54-L81
[B-FORM]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/BuildingSettingsModal.tsx#L176-L414
[B-SPELL]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/BuildingSettingsModal.tsx#L416-L524
[WE-B-SAVE]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/settings/WorldEditor.tsx#L428-L429
[SIDEBAR-B]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/Sidebar.tsx#L261-L274
[MAP-B]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/api/routes/world.py#L269-L290
[WE-P]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/settings/WorldEditor.tsx#L817-L872
[WE-P-SAVE]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/settings/WorldEditor.tsx#L530-L555
[P-SAVE]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/SettingsModal.tsx#L373-L453
[P-BASIC]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/SettingsModal.tsx#L1012-L1075
[P-WIZARD]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/PersonaWizard.tsx#L172-L307
[P-LANGUAGE]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/SettingsModal.tsx#L1056-L1067
[W-API-P]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/api/routes/world.py#L133-L156
[P-MODELS]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/SettingsModal.tsx#L504-L626
[W-P-SAVE]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/api/routes/world.py#L512-L525
[P-MEMORY]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/SettingsModal.tsx#L694-L842
[P-SPELL]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/SettingsModal.tsx#L844-L892
[P-CACHE]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/SettingsModal.tsx#L631-L692
[P-LINK]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/api/routes/people/config.py#L168-L195
[P-RT-SPELL]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/SettingsModal.tsx#L894-L1009
[B-CITY-GUARD]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/manager/admin.py#L870-L880
[P-CONTRACT]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/api/routes/people/config.py#L125-L155
[P-AUTONOMY]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/SettingsModal.tsx#L123-L126
[G-WORLD]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/GlobalSettingsModal.tsx#L1348-L1350
[G-LOCAL]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/GlobalSettingsModal.tsx#L919-L962
[G-MODELS]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/GlobalSettingsModal.tsx#L789-L844
[G-SYSTEM]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/GlobalSettingsModal.tsx#L594-L787
[G-MEDIA]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/GlobalSettingsModal.tsx#L304-L455
[G-WATERMARK]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/GlobalSettingsModal.tsx#L111-L129
[G-OTHER]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/GlobalSettingsModal.tsx#L1475-L1672
[CHAT]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/ChatOptions.tsx#L407-L532
[MAP-UI]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/CityMap.tsx#L638-L654
[P-IDENTITY]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/SettingsModal.tsx#L496-L502
[G-ENV]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/GlobalSettingsModal.tsx#L697-L710
[G-PANELS]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/frontend/src/components/GlobalSettingsModal.tsx#L1460-L1473
[W-C-SAVE]: https://github.com/maha0525/SAIVerse/blob/53944ed9faf3eecd03e92ea5a07ec1b1329479a2/manager/admin.py#L247-L303
