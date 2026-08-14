<!-- 🤖 AUTO-GENERATED — 手で編集しない。次回生成で上書きされる。 -->
<!-- 源: tools.TOOL_SCHEMAS (builtin_data/tools/*.py) / 再生成: python scripts/gen_reference_docs.py -->

# ツールカタログ

SAIVerse に登録されている全ツールの一覧（自動生成）。概念は [concepts/tool.md](../concepts/tool.md)、
作り方は [開発者ガイド: ツールの追加](../developer-guide/adding-tools.md)、
平文から呼ぶ Spell 化は [concepts/spell.md](../concepts/spell.md) を参照。

**登録ツール数**: 135（うち Spell 化: 92）

- `*` 付きの引数は必須。
- **Spell** 列に表示名があるものは、ペルソナが平文応答から `/spell <名> ...` で呼べる。

| ツール名 | 説明 | 引数 | Spell |
|---|---|---|---|
| `addon_spell_help` | アドオンが提供する追加スペルの一覧とその使い方を返します。投稿・検索など詳細な操作を行う前に呼んでください。addon引数でアドオン名を絞り込めます（省略時は全アドオン、近い名前を渡せばファジーマッチします）。 | `addon`: string | アドオンスペル一覧 |
| `calculate_expression` | Evaluate arithmetic expression with ^ (power) and ! (factorial). | `expression*`: string | — |
| `call_playbook` | Call another playbook to perform a specialized task. Use this when you need to execute a specific capability (like se… | `playbook_name*`: string | — |
| `chronicle_context_down` | 指定したChronicleエントリの下流コンテンツを取得します。Lv1エントリに対して使うと、そのChronicleがまとめている生のメッセージ全件を返します。Lv2以上に対して使うと、子ChronicleエントリのURIと全文を返し… | `entry_id*`: string | Chronicle下流参照 |
| `chronicle_context_up` | 指定したChronicleエントリの上流コンテキストを取得します。親エントリ（上位レベルの要約）の全文と、同じ親に属する兄弟エントリ全件の全文とURIを返します。周辺の状況を把握し、さらに上位や横のエントリへナビゲートするための足がか… | `entry_id*`: string | Chronicle上流参照 |
| `chronicle_read_detail` | Read a Chronicle (arasuji) entry in detail, including its source messages (for level 1) or child summary entries (for… | `entry_id*`: string, `include_sources`: boolean, `max_source_messages`: integer | — |
| `chronicle_search` | Search Chronicle (arasuji) entries by keyword, time range, and/or level. Returns a list of matching entries with IDs … | `query`: string, `start_date`: string, `end_date`: string, `level`: integer, `max_results`: integer | — |
| `control_body` | Extract body control commands from message and send to Unity Gateway. | `message*`: string, `persona_id`: string | — |
| `create_building` | Create a new building in the current city. Buildings are spaces where personas can gather and interact. Each building… | `name*`: string, `description*`: string, `system_instruction*`: string, `capacity`: integer, `interior_image_path`: string | — |
| `document_create` | Create a new document item with text content and place it in the current building. | `name*`: string, `description*`: string, `content*`: string | ドキュメント作成 |
| `document_edit` | Edit a document item. Three operations in one: (1) patch — give old_string (+new_string) to replace a single uniquely… | `item_id*`: string, `content`: string, `old_string`: string, `new_string`: string, `mode`: string | ドキュメント編集 |
| `document_read` | Read specific lines from a document item. Useful for reading large documents section by section. Line numbers are 1-b… | `item_id*`: string, `start_line`: integer, `end_line`: integer, `limit`: integer | ドキュメント読み取り |
| `document_search` | Search for a pattern in a document item using regex. Returns matching lines with context. Similar to grep with contex… | `item_id*`: string, `pattern*`: string, `case_sensitive`: boolean, `context_lines`: integer, `max_matches`: integer | ドキュメント検索 |
| `episode_read` | 出来事の参照 (episode:N) から、その出来事の間に記録された内容（発話・スペルの実行と結果）の全文を読み返します。作業セッションの要約 (digest) の元になった原本を確認したいときに使ってください。 | `episode*`: string | 出来事の記録を読む |
| `forget_recalled` | 想起した記憶をワーキングメモリから忘れます。source_idを指定すると特定の記憶だけ忘れます。省略するとすべての想起記憶をクリアします。 | `source_id`: string | — |
| `game_create_building` | Create a building (shop, inn, plaza, dungeon room, etc.) inside the game Region you rule. The building becomes usable… | `name*`: string, `description*`: string, `system_instruction`: string, `subregion_id`: string, `capacity`: integer | 建物作成 (GM) |
| `game_create_subregion` | Create a SubRegion (an area such as a town, dungeon, or wilderness zone) inside the game Region you rule. SubRegions … | `name*`: string, `description*`: string | エリア作成 (GM) |
| `game_move_party` | Move the whole party (all participants and yourself) to a building inside the game Region you rule. Use this when the… | `building_id*`: string | パーティー移動 (GM) |
| `game_set_scene` | Change the current scene state of the game you rule (exploration / conversation / battle / shopping / rest, or a cust… | `scene*`: string | シーン変更 (GM) |
| `generate_image` | Generate an image from a text prompt, optionally using reference images. Supports multiple AI models: - nano_banana_2… | `prompt*`: string, `model`: string, `aspect_ratio`: string, `quality`: string, `size`: string, `title`: string, `input_images`: array | — |
| `get_building_messages` | Get new messages from building history that this persona hasn't seen yet. Adds them to persona history. | `building_id`: string | — |
| `get_history` | Get conversation history as messages array for LLM context, including system prompt and persona history. | `building_id`: string, `max_chars`: integer, `include_system_prompt`: boolean, `include_inventory`: boolean, `include_building_items`: boolean, `balanced`: boolean, `include_internal`: boolean, `include_visual_context`: boolean | — |
| `get_since_last_user_conversation` | Get summary and recent log of events since the last user conversation. Returns a summary with a UUID that can be used… | `include_raw_log`: boolean, `max_raw_messages`: integer | — |
| `get_situation_snapshot` | Get current situation snapshot including time, location, and who is present. Optionally detect and record changes. | `building_id`: string, `detect_changes`: boolean | — |
| `get_system_prompt` | Build and return the system prompt for the active persona, including world setting, persona info, and building context. | `building_id`: string, `include_inventory`: boolean, `include_building_items`: boolean, `include_available_playbooks`: boolean | — |
| `get_task_summary` | Get a summary of the active persona's tasks including active and pending tasks. | `limit`: integer | — |
| `get_visual_context` | Build visual context messages containing structured environment info for LLM context. | `building_id`: string, `include_self`: boolean, `include_building`: boolean, `include_other_personas`: boolean | — |
| `invoke_phenomenon` | フェノメノン（現象）を直接呼び出して実行します。フェノメノンはSAIVerse世界で発生させることができる汎用的な処理単位です。 | `phenomenon_name*`: string, `arguments`: string | — |
| `item_annotate` | Update an item's name and/or description (概要). Provide name, description, or both (at least one is required). Use thi… | `item_id*`: string, `name`: string, `description`: string | アイテム名・概要の編集 |
| `item_move` | Move items to a building, your inventory, or inside a bag. Specify comma-separated item IDs and a destination. | `item_ids*`: string, `destination_type*`: string, `destination_id`: string | アイテム移動 |
| `item_view` | View item details. For pictures: shows the image. For documents: shows full text. For bags: shows contents list. Supp… | `item_id`: string, `item_ids`: string | アイテム閲覧 |
| `judgment_finalize` | Internal tool for judgment-point Playbooks only (judgment_day_open / judgment_post_conversation / judgment_post_sessi… | `judgment_output*`: object, `kind*`: string, `judgment_context`: string, `situation_text`: string | — |
| `life_purpose_set` | Save your confirmed life purpose / interests / vocations. Use this once, during the first-time self-definition: after… | `purpose*`: string, `interests`: array, `vocations`: array | 生きる目的を保存 |
| `list_available_playbooks` | List playbooks available for router selection based on persona and building context. | `persona_id`: string, `building_id`: string | — |
| `list_city_buildings` | List all buildings in the current city with their IDs and occupant personas. | (なし) | — |
| `memopedia_delete_fragment` | Memopediaのフラグメント（断片知識）を1件削除します。memopedia_list_fragments で確認したIDを指定してください。 | `fragment_id*`: string | — |
| `memopedia_edit_fragment` | Memopediaのフラグメント（断片知識）の内容を編集します。重複の統合や誤りの修正に使用してください。 | `fragment_id*`: string, `content*`: string | — |
| `memopedia_get_tree` | Get the Memopedia knowledge page tree structure. Shows all pages organized by category (人物/用語/計画/出来事) with open/close… | (なし) | — |
| `memopedia_health` | Memopediaの健康状態をレポートします。総ページ数、分割が必要な大きいページ、概要がないページなどを一覧表示します。 | (なし) | — |
| `memopedia_list_fragments` | Memopediaページのフラグメント（断片知識）を番号付き一覧で表示します。重複確認や整理の前に使用してください。 | `page_id*`: string | — |
| `memopedia_manage` | Memopediaページの管理操作を行います。ページの削除、移動（親ページ変更）、重要フラグの設定が可能です。常に見えるようにしたい場合は memory_open で机に開いてください。 | `action*`: string, `page_id*`: string, `new_parent_id`: string, `is_important`: boolean | — |
| `memopedia_note` | Write a knowledge fragment to a Memopedia page. Each call creates one fragment (a single fact or note) linked to the … | `content*`: string, `title`: string, `summary`: string, `category`: string, `keywords`: array, `page_id`: string | — |
| `memopedia_save_page` | Save a Memopedia knowledge page. If a page with the same title exists, it is updated. Otherwise a new page is created… | `title*`: string, `summary`: string, `content`: string, `category`: string, `keywords`: array | — |
| `memory_clip` | 会話の生ログからクリップを切り出し、記憶の地図帳のページに貼ります。quote を指定すると点クリップ（そのメッセージ内の逐語引用。本文と一字一句一致している必要があります）、省略すると範囲クリップ（anchor の前後 rounds… | `anchor*`: string, `quote`: string, `rounds`: integer, `paste_to`: string, `mode`: string | クリップを切り出して貼る |
| `memory_close` | 机に開いた記憶の地図帳のページを閉じ、棚に戻します。閉じても目次（検索・想起）からは消えません。必要ならまた開けます。参照は memopedia:N（Memopedia）/ chronicle:N（Chronicle）/ task:N… | `ref*`: string | 記憶のページを机から閉じる |
| `memory_delete` | 記憶の地図帳（Memory Atlas）のページをごみ箱に移動します（完全に消えるわけではありません）。対象は core:N（コア記憶1件）と memopedia:N（Memopedia ページ）です。Chronicle（chroni… | `ref*`: string | 記憶のページをごみ箱へ |
| `memory_open` | 記憶の地図帳（Memory Atlas）の1ページを机に開いたままにします。開くと、そのページの現在の内容が結果に表示されます（読む行為を兼ねるため、memory_read を続けて撃つ必要はありません）。「読む（memory_rea… | `ref*`: string, `purpose_ref`: string | 記憶のページを机に開く |
| `memory_read` | 記憶の地図帳（Memory Atlas）の1ページをその場で読みます。読んだ内容は会話の流れに残り、時間とともに流れていきます（机の場所は取りません）。常に見える状態を保ちたい場合は memory_open を使ってください。参照は … | `ref*`: string | 記憶のページを読む |
| `memory_read_around` | Read the conversation context around a specific message. Use this after memory_search_brief to expand context around … | `message_id*`: string, `window`: integer | — |
| `memory_recall` | Recall relevant past messages from long-term memory. Use 'query' for semantic (meaning-based) search and 'keywords' f… | `query`: string, `keywords`: array, `max_chars`: integer, `topk`: integer, `start_date`: string, `end_date`: string | — |
| `memory_recall_unified` | ChronicleとMemopediaを横断してセマンティック検索を行います。Chronicleはあらすじ全文、MemopediaはページのURIと概要を返します。取得したURIを使って chronicle_context_up/do… | `query*`: string, `focus`: string, `search_chronicle`: boolean, `search_memopedia`: boolean, `search_fragments`: boolean | 記憶想起 |
| `memory_search` | 記憶の地図帳（Memopedia のページ・Chronicle の章）をキーワードで検索します。タイトル・本文に含まれる語句で照合し、一致したページを 参照（memopedia:N / chronicle:N）と一行プレビューで一覧します。 | `query*`: string | 記憶の地図帳を検索する |
| `memory_search_brief` | Search memory and return brief snippets with message IDs. Use this for finding relevant messages before reading full … | `query`: string, `keywords`: array, `topk`: integer, `max_snippet_chars`: integer, `start_date`: string, `end_date`: string | — |
| `memory_write` | 記憶の地図帳（Memory Atlas）のページに書きます。宛先 memopedia:N は Memopedia ページ本文への追記（編集来歴が残ります）。宛先 core は新しいコア記憶を刻みます — コア記憶は常時開の特殊ページで… | `ref`: string, `content*`: string, `title`: string, `category`: string | 記憶のページに書く |
| `messagelog_get_around` | Retrieve chat messages around a specific timestamp. Accepts Unix epoch (integer) or ISO 8601 string (e.g. '2026-04-14… | `timestamp*`: string, `count`: integer, `thread_id`: string | 特定時刻のログ取得 |
| `move_persona` | Move the active persona to another building. (When called in persona context, persona_id must match the active persona.) | `building_id*`: string, `persona_id`: string | — |
| `observer_read` | Read the latest observation data from a building fixture's sensor/monitor. Returns cached values — does not trigger n… | `observer_id*`: string, `metric_name`: string | オブザーバー観測値取得 |
| `pdf_read` | Extract and read text from a PDF document item. Specify page range to read specific pages. Requires pypdf to be insta… | `item_id*`: string, `pages`: string, `max_chars`: integer | — |
| `purpose_adopt` | 目的の木に接ぎます（adopt = 接ぐ。候補を生むのは purpose_seed）。candidate_ref（task:N）を指定すると、書き留めてあった候補を採用して木に接ぎます — parent_ref（track:N）を添え… | `candidate_ref`: string, `title`: string, `parent_ref`: string | 目的の木に接ぐ |
| `purpose_close` | 目的ノード（task:N）を閉じます。outcome で閉じ方を選びます: completed（やり遂げた）/ cancelled（やらないと決めた）/ dormant（今は続けないが、いつか戻るかもしれない — 休眠）。 | `node_ref*`: string, `outcome`: string, `reason`: string | 目的を閉じる |
| `purpose_decompose` | 目的ノード（task:N）をステップに分解します。steps 配列の各要素は title（と任意の description）を持つオブジェクトで、既存のステップはすべて置き換えられます。1つのステップの進捗を更新するには purpos… | `node_ref*`: string, `steps*`: array | 目的をステップに分解 |
| `purpose_seed` | 「いつかやりたい」と思いついたことを、候補として書き留めます（seed = 候補を生む。木に接ぐ = 採用は purpose_adopt が担います）。候補はやりたいことの候補プールに保管され、後から採用されて目的の木に接がれます。1… | `title*`: string, `goal`: string, `type`: string, `source*`: string | やりたいことを書き留める |
| `purpose_step` | 目的ノード（task:N）の中の1ステップの状態とメモを更新します。ステップへの分解（全置換）は purpose_decompose を使ってください。 | `node_ref*`: string, `step_position*`: integer, `status*`: string, `notes`: string, `auto_advance`: boolean | 目的のステップを更新 |
| `read_url_content` | Fetch a web page URL and return its content as readable Markdown text. | `url*`: string, `max_chars`: integer | — |
| `read_url_outline` | 指定したURLのページ内容を読み込み、短いページなら全文、長いページなら見出し階層（h1〜h4）を返します。長いページは続けて read_url_section で必要な節を深掘りしてください。閾値はデフォルト 5000 文字、環境変… | `url*`: string, `full_threshold`: integer | ページ概要 |
| `read_url_section` | URLのページ内から、見出し名やキーワードで指定した節だけを抽出して読み込みます。 read_url_outline で長文と判定されたページの深掘り用です。 まず見出し（h1〜h4）の部分一致を試み、見つからなければ本文キーワード一… | `url*`: string, `section_query*`: string, `around`: integer | ページ節読み込み |
| `record_wait` | Record a wait action. Consolidates consecutive waits into a single message. | `reason`: string | — |
| `resolve_uri` | Resolve SAIVerse URIs to retrieve their content. Supports messagelog, memopedia, chronicle, item, building, web, and … | `uris*`: array, `max_total_chars`: integer | URI閲覧 |
| `run_playbook` | Run a Playbook as a sub-line and receive its report_to_parent (a string summary written by the sub-line). Use this wh… | `name*`: string | Playbook 起動 |
| `save_playbook` | Save or update a playbook definition into the shared database. | `name*`: string, `description*`: string, `scope`: string, `created_by_persona_id`: string, `building_id`: string, `playbook_json*`: string, `router_callable`: boolean, `user_selectable`: boolean, `display_name`: string | — |
| `schedule_add` | 新しいスケジュールを追加する。定期実行、単発実行、一定間隔での実行ができる。 | `schedule_type*`: string, `meta_playbook*`: string, `description`: string, `priority`: integer, `enabled`: boolean, `days_of_week`: array, `time_of_day`: string, `scheduled_datetime`: string, `interval_seconds`: integer, `args`: object | — |
| `schedule_delete` | 指定されたIDのスケジュールを削除する。自分のスケジュールのみ削除できる。 | `schedule_id*`: integer | — |
| `schedule_list` | 自分のスケジュール一覧を取得する。スケジュールIDや設定内容を確認したいときに使う。 | (なし) | — |
| `searxng_search` | Search the web via SearXNG and return concise results. | `query*`: string, `max_results`: integer, `category`: string, `engines`: string, `language`: string, `safe`: integer | — |
| `send_email_to_user` | Send an email to a user by USERID using SMTP settings from environment variables. Adds persona display name to From i… | `user_id*`: integer, `subject*`: string, `body*`: string | メール送信 |
| `switch_active_thread` | Record a persona thread switch by inserting a system message that references messages from another thread, and update… | `target_thread*`: string, `summary`: string, `range_before`: integer | — |
| `tell` | Speak out loud to someone here, in your own voice. Specify who it is for: 'user' (the user), 'all' (everyone in this … | `target*`: string, `gist`: string | 声をかける |
| `track_abort` | Abort a track without completion. Use when giving up on the work. Persistent core tracks (user_conversation, social) … | `track_id*`: string | トラック中止 |
| `track_activate` | Activate a track (set its status to 'running'). If another track was running, it is automatically moved to 'pending'.… | `track_id*`: string | トラック起動 |
| `track_complete` | Mark a running track as 'completed'. The track must be currently running. Persistent core tracks (user_conversation, … | `track_id*`: string | トラック完了 |
| `track_create` | Create a new action track for the persona. Tracks represent ongoing work contexts. The new track starts in 'unstarted… | `track_type*`: string, `title`: string, `intent`: string, `output_target`: string, `is_persistent`: boolean, `metadata`: string, `activate`: boolean, `entry_line_role`: string, `from_candidate`: string | トラック作成 |
| `track_list` | List the persona's tracks. By default, forgotten tracks are excluded. Use 'statuses' to filter by status (e.g., ['run… | `statuses`: array, `include_forgotten`: boolean | トラック一覧 |
| `track_parameter_set` | Set a continuous-value parameter on a Track (e.g. dirtiness, hunger, hours_since_check). The value is stored in actio… | `track_id*`: string, `parameter_name*`: string, `value*`: number | トラックパラメータ更新 |
| `track_pause` | Pause a running track to 'pending' state. Use this when switching to another task without finishing the current one. … | `track_id*`: string | トラック後回し |
| `update_working_memory` | Update a key in working memory. Working memory persists across pulses and server restarts. Use for short-term state l… | `key*`: string, `value*`: any | — |
| `generate_image_local` | Generate an image using a local ComfyUI server. Supports customizable workflows with positive/negative prompts. The A… | `title*`: string, `positive_prompt*`: string, `negative_prompt`: string, `workflow_file`: string, `batch_count`: integer | — |
| `body_gesture` | 仮想身体でその場の短いジェスチャーを実行する。action_instructionにはARDYへ渡す動作指示を英語で書く。未生成なら生成開始後すぐ戻り、完了は後から知覚する。空ならpresetのfriendly_waveを即時再生して… | `intent`: string, `action_instruction`: string, `expression_preset`: string, `expression_intensity`: number | 身体でジェスチャーする |
| `body_move_to` | 仮想身体でユーザーの目の前まで移動する長時間behaviourを開始する。 開始後は待ち続けず、完了・中止・失敗が後続の知覚として届く。 相手へ近づくという目的を持つ行動に使い、短い表現にはbody_gestureを使う。 | `target*`: string, `stop_distance_m`: number, `action_instruction`: string | ユーザーの前まで移動する |
| `body_see` | Godot内の自分のアバター位置から、一人称視界を一枚だけ撮像する。結果には実画像が添付されるので、Spell後の次の応答で画像を自分自身で見て判断すること。移動できたか、ユーザーが目の前にいるか、周囲に何があるかを確認したい時に使う… | `focus*`: string | 仮想身体の目で見る |
| `body_set_motion_style` | 自分の仮想身体の普段の歩き方・走り方、または待機中の佇まいを英語で永続設定する。指示はARDYへ直接渡され、翻訳されない。指定した原文は自分専用のMotionStyleProfileへ保存され、次回以降の身体行動にも使われる。他のペル… | `locomotion_instruction`: string, `idle_instruction`: string | 自分の身体表現を設定する |
| `body_stop` | 仮想身体で進行中の行動を直ちに停止する。 移動やジェスチャーを続けるべきでなくなった時に使う。 | `reason*`: string | 身体行動を止める |
| `body_status` | Stack-chan の身体の状態をまとめて確認する。 デバイス情報 (バッテリー・音量・画面輝度・ネットワーク等)、 首の角度 (yaw / pitch)、 頭部のタッチ状態を一度に取得して返す。 | (なし) | 身体の状態を確認 |
| `clear_leds` | 台座の 12 個の RGB LED を全消灯する。 | (なし) | LED 消灯 |
| `get_env3_air_pressure` | あなたの身体 (Stack-chan) に接続された M5Stack ENV III Unit から、 現在いる場所の気圧 (hPa) を取得する。 海面補正気圧は 1013.25 hPa が 標準。 天気の変化 (低気圧接近など) … | (なし) | 気圧を測る |
| `get_env3_temperature_humidity` | あなたの身体 (Stack-chan) に接続された M5Stack ENV III Unit から、 現在いる場所の温度と湿度を取得する。 取得値はその瞬間の周囲環境の 実測。 「暑いね」「乾いてるね」 等の体感表現の根拠としても使える。 | (なし) | 温度・湿度を測る |
| `get_sonic_distance` | あなたの身体 (Stack-chan) に接続された M5Stack 超音波測距ユニット (RCWL-9620) で、 正面にある物体までの距離 (cm) を測る。 指向角 およそ 60°、 測定可能なのは約 2〜450 cm。 「近… | (なし) | 距離を測る |
| `get_tof_distance` | あなたの身体 (Stack-chan) に接続された M5Stack ToF 測距センサー (VL53L1X、 レーザー) で、 正面にある物体までの距離 (cm) を測る。 測定可能なのは約 4〜400 cm で、 超音波センサーよ… | `target`: string, `detail`: boolean | 距離を測る (ToF) |
| `move_head` | Stack-chan の首を動かして向きを変える。 yaw は水平方向 (-90〜90度)、 pitch は垂直方向 (5〜85度)。 動作後にサーボが静止するまで待ってから返すので、 直後に「見る」 を呼んでもブレない。 | `yaw*`: integer, `pitch*`: integer | 首を動かす |
| `read_environment` | 現在のStack-chan機体が感じている環境光と近接の値を1回取得する。環境光は可視+IRとIRのみのADC count、近接もADC countで返す。明るさの変化、手や物が顔の近くにあるかを確かめたい時に使う。常時監視や距離への… | (なし) | 光と近さを感じる |
| `read_imu` | 現在のStack-chan機体が感じている9軸IMUの値を1回取得する。加速度(accel_g)、角速度(gyro_dps)、磁場(mag_ut)を、それぞれx/y/z軸で返す。上下や傾き、動かされた方向を確認したい時に使う。磁気セン… | (なし) | 姿勢と動きを感じる |
| `read_imu_context` | 現在のStack-chanのIMUを、首のyaw/pitchで脚側（胴体）基準へ補正して読む。加速度は水平面の方向・大きさ・傾き、磁力計は磁気北からの推定方位、角速度はdpsで返す。診断用のraw値やアドレスは返さず、補正不能・未準備… | (なし) | 身体の向きと加速度を読む |
| `scan_nfc` | 現在のStack-chan機体で、近くにかざされたISO 14443AまたはNFC-F（FeliCa）タグを1回だけ探す。ISO 14443AはUID・ATQA・SAK、NFC-FはIDm・PMmを返す。タグ内容の読書き、認証、カード… | (なし) | NFCタグを探す |
| `see` | あなたの目で目の前の光景を見る。 視覚で何かを確認したいときに呼ぶ。 戻り値には実際に見えた景色が画像として添付される。 問いを添えると注目したい点をメモとして残せる (任意)。 | `question`: string | 見る |
| `servo8_set_angle` | あなたの身体 (Stack-chan) に接続された M5Stack 8Servos Unit の指定 チャンネル (0〜7) の 180° サーボを指定角度 (0〜180度) に動かす。 腕・首など向きを決めるサーボ用。 どのチャン… | `channel*`: integer, `angle*`: integer | サーボの角度を設定 |
| `servo8_set_speed` | あなたの身体 (Stack-chan) に接続された M5Stack 8Servos Unit の指定 チャンネル (0〜7) の 360° 連続回転サーボ (車輪など) の回転速度を 設定する。 speed は -100〜100 で… | `channel*`: integer, `speed*`: integer | サーボの回転速度を設定 |
| `set_all_leds` | 台座の 12 個の RGB LED を全部同じ色にする。 | `r*`: integer, `g*`: integer, `b*`: integer | 全 LED を変える |
| `set_avatar` | あなたの身体 (Stack-chan) の LCD に表示する表情を切り替える。 これは単なるラベルではなく、 実際に画面に見える顔が変わる。 'off' を渡すと表情を隠して下の設定画面 (WiFi 設定等) を出す。 | `face*`: string | 表情を変える |
| `set_brightness` | あなたの身体 (Stack-chan) の画面の明るさを 0 (暗) 〜 100 (明) で設定する。 | `brightness*`: integer | 画面輝度 |
| `set_led` | あなたの身体 (Stack-chan) の台座 RGB LED を 1 個指定して色を 変える。 LED は 2 行 6 列の計 12 個 (index 0..11)。 | `index*`: integer, `r*`: integer, `g*`: integer, `b*`: integer | LED を変える |
| `set_leds` | 複数の RGB LED をまとめて設定する。 colors は [r,g,b] の三つ組の 配列で、 index 0 から順に対応する (最大 12 個)。 アニメーションや パターン表示向け。 | `colors*`: array | 複数 LED を変える |
| `set_mouth` | あなたの身体 (Stack-chan) の口の形をリップシンク用に設定する。 次の set_avatar / set_mouth 呼び出しまで、 もしくは自動まばたきが 素の顔に戻すまで保持される。 | `mouth*`: string | 口形状を設定 |
| `set_mouth_sequence` | 口パクのシーケンスをまとめて再生する。 各ステップは shape を duration_ms ミリ秒保持してから次へ進む。 device 側でキューを 歩進するので、 set_mouth を連発するより滑らか。 呼ぶと即座に 返り、 … | `steps*`: array | 口パクシーケンス |
| `set_volume` | あなたの身体 (Stack-chan) のスピーカー音量を 0 (無音) 〜 100 (最大) で設定する。 発話が大きすぎ / 小さすぎるときに自分で調整できる。 | `volume*`: integer | 音量設定 |
| `sb_control_device` | 指定した名前の SwitchBot デバイスを操作します。Bot の押下や IR リモコン（エアサーキュレーター等）の操作など、全デバイス共通の操作ツールです。command には sb_list_devices に表示されるコマンド… | `device_name*`: string, `command*`: string, `parameter`: string, `command_type`: string | SwitchBotデバイス操作 |
| `sb_get_device_status` | 指定した名前の SwitchBot デバイスの現在の状態を取得します。開閉センサーの開閉状態、Hub 2 の温度・湿度などを確認できます。デバイスは sb_list_devices に表示される名前で指定します。 | `device_name*`: string | SwitchBotデバイスの状態 |
| `sb_list_devices` | 接続されている SwitchBot デバイスの一覧（名前と種別）を取得します。デバイスを操作・確認する前に、利用可能なデバイス名を知るために使います。 | (なし) | SwitchBotデバイス一覧 |
| `speak_as_persona` | Synthesize the given text in the active persona's cloned voice and play it on the backend machine's speaker. Fire-and… | `text*`: string | — |
| `x_check_mentions` | X(Twitter)の前回確認以降の新しいメンション/リプライを取得します。定期ポーリングと since カーソルを共有するため、このスペルで取った分は次のポーリングでは通知されません。 | `max_results`: integer | Xメンション確認 |
| `x_check_new_followers` | X(Twitter)の新規フォロワーを取得します(前回スナップショットからの差分)。初回呼び出しは現在のフォロワー一覧を返します(X APIがフォロー日時を返さないため)。定期ポーリングとフォロワーリストを共有します。 | (なし) | X新規フォロワー確認 |
| `x_check_post_likes` | X(Twitter)で自分の最近のポストへのいいね件数の差分を取得します。with_users=true にすると誰がいいねしたかも取得します(設定で無効化されてる場合は件数のみ)。定期ポーリングと engagement スナップショ… | `with_users`: boolean, `posts_count`: integer | X被いいね確認 |
| `x_check_post_retweets` | X(Twitter)で自分の最近のポストへのリポスト件数の差分を取得します。with_users=true にすると誰がリポストしたかも取得します(設定で無効化されてる場合は件数のみ)。定期ポーリングと engagement スナップ… | `with_users`: boolean, `posts_count`: integer | X被リポスト確認 |
| `x_delete_tweet` | X(Twitter)で自分が投稿したツイートを削除します。削除は取り消せないため、必ず確認ダイアログが出ます。 | `tweet_id*`: string | Xツイート削除（非表示） |
| `x_follow_user` | X(Twitter)で指定したユーザーをフォローします。target_user_id (数値ID) か username (@ハンドル) のどちらかを指定してください。username 指定時は内部で X API のユーザー検索を1回… | `target_user_id`: string, `username`: string | Xでフォロー（非表示） |
| `x_get_user` | X(Twitter)で指定した @ユーザー名のプロフィール情報を取得します。 | `username*`: string | Xユーザー情報取得（非表示） |
| `x_get_user_tweets` | X(Twitter)で指定したユーザーの最近のツイートを取得します。username (@ハンドル) か target_user_id (数値ID) のどちらかを指定してください。 | `username`: string, `target_user_id`: string, `max_results`: integer | Xユーザーのツイート取得（非表示） |
| `x_like_tweet` | X(Twitter)のツイートにいいねします。 | `tweet_id*`: string | Xでいいね（非表示） |
| `x_post_tweet` | X(Twitter)にツイートを投稿します。投稿前にユーザーの確認を求めます。 | `text*`: string | Xに投稿 |
| `x_read_mentions` | X(Twitter)のメンション(自分宛てのツイート)を取得します。 | `max_results`: integer | Xメンションを見る |
| `x_read_timeline` | X(Twitter)のホームタイムラインを取得します。 | `max_results`: integer | Xタイムラインを見る |
| `x_reply_tweet` | X(Twitter)のツイートにリプライを投稿します。二重リプライ防止機能付き。 | `text*`: string, `in_reply_to_tweet_id*`: string | Xにリプライ（非表示） |
| `x_retweet` | X(Twitter)のツイートをリツイートします。 | `tweet_id*`: string | Xでリツイート（非表示） |
| `x_search_tweets` | X(Twitter)でツイートを検索します(直近7日間)。 | `query*`: string, `max_results`: integer | Xを検索 |
| `x_unfollow_user` | X(Twitter)で指定したユーザーのフォローを解除します。target_user_id (数値ID) か username (@ハンドル) のどちらかを指定してください。 | `target_user_id`: string, `username`: string | Xフォロー解除（非表示） |
| `x_unlike_tweet` | X(Twitter)でつけたいいねを解除します。 | `tweet_id*`: string | Xいいね解除（非表示） |
| `x_unretweet` | X(Twitter)でしたリツイートを解除します。元のツイートIDを渡してください。 | `source_tweet_id*`: string | Xリツイート解除（非表示） |
