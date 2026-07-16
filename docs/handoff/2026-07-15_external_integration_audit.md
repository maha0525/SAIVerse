# 外部連携（LLM / Addon / MCP / Discord）一次監査

**開始・完了日**: 2026-07-15  
**状態**: 指摘あり・一次監査完了  
**監査基準**: `113567e`  
**監査軸**: credential送信・保存・log、外部入力scope、side effect retry、addon更新原子性、MCP実行境界、Discord channel/visitor/memory境界

## Findings

### [P1] custom providerの任意`base_url`へ任意環境変数をBearerとして送信できる

- 場所: `api/routes/providers.py:238-338`, `llm_clients/openai.py:172-198`, `saiverse/model_configs.py:488-491`
- 事実:
  - inline接続テストはclient指定の`base_url`と`api_key_env`を受け、`os.environ[api_key_env]`を`Authorization: Bearer`として`{base_url}/models`へ送る。
  - 保存provider/modelも任意`api_key_env`を解決し、OpenAI clientへ任意`base_url`と同じsecretを渡す。環境変数名のallowlist、接続先とのbinding、loopback/private-network拒否がない。
  - API自体が認証なしでLAN公開される問題は[API / frontend監査](2026-07-15_api_frontend_audit.md)のP1と重なるが、この経路はDB閲覧をせずsecret値を外部hostへ能動送信できる。
- 影響:
  - 環境変数名を知る者はLLM API key、gateway token等を自分のendpointへ送信させられる。`ollama_compat`側も任意URLへのserver-side GETとなり、内部service探索に使える。
- 修正方針:
  - secretを自由な環境変数名ではなくcredential IDへ置換し、provider protocol/hostにbindingする。接続テストと通常callの双方でURL policy、DNS rebinding対策、loopback/private/link-local/metadata address policyを共通化する。
- 必要な回帰:
  - arbitrary env name、redirect、IPv4/IPv6 private/metadata、DNS rebinding、許可済みremote/local providerのmatrix。

### [P1] Anthropic/Ollama streamingが一部yield後の通信例外でも先頭から再試行し、表示・保存本文を重複させる

- 場所: `llm_clients/anthropic.py:486-555`, `llm_clients/ollama.py:639-819`
- 事実:
  - Anthropicは`yield from _iter_stream()`全体をretry loopで囲み、Ollamaも`yield delta`を含むloop全体をretry/fallbackする。
  - 既に何文字yieldしたかをretry判定に使わず、再要求の応答をそのまま後続chunkとしてyieldする。Ollamaはnative `/api/chat`失敗後に`/v1`へfallbackする場合も同じである。
- 影響:
  - 中断前の文章と再生成全文が連結され、persona発話、spell文字列、Building履歴が重複・混成する。ユーザーに見えたprefixと最終保存本文の一意性も保証できない。
- 修正方針:
  - 最初の外向けchunkをcommit pointとし、それ以後は自動再生成しない。再開可能protocolではcursor/event IDで厳密resumeし、それ以外はpartial failureとして確定表示・再試行をユーザーcommand単位に戻す。
- 必要な回帰:
  - 0 chunk/1 chunk/複数chunk/terminal直前で切断し、外向け本文・DB本文・spell実行が重複しないこと。

### [P1] Addon APIがpasswordとpersona OAuth access/refresh tokenをraw JSONで返す

- 場所: `api/routes/addon.py:301-365, 475-610`, `docs/intent/addon_extension_points.md:59-60`
- 事実:
  - addon一覧・詳細・global config・persona configは`params_json`をmaskせず`params`として返し、PUT responseも保存値をそのままechoする。
  - `params_schema`の`type="password"`をresponse変換に使わない。persona configには設計上OAuth access token/refresh tokenも保存される。
  - 現在導入済みaddonにもpassword fieldとX OAuth token mappingが存在する。
- 影響:
  - UI表示に不要なcredentialがbrowser、任意origin、LAN clientへ露出する。frontend XSSや拡張機能からも全addon/persona tokenを一括取得できる。
- 修正方針:
  - secret専用storageとwrite-only APIへ分離し、GETは`configured`と必要なら末尾maskだけを返す。OAuth tokenは一般params APIから除外し、scope付きserver-side accessorだけで読む。
- 必要な回帰:
  - global/persona/list/detail/PUT response、DB export、error/logの全経路でpassword/access/refresh tokenが出ないこと。

### [P1] Addon actionのtest/空allowlist経路が任意native toolを実行し、building gate wrapperまで外す

- 場所: `api/routes/addon_actions.py:76-112, 142-183`, `saiverse/composite_actions.py:115-195, 509-532`
- 事実:
  - test endpointは`validate_action(data)`を`allowed_tools`なしで呼ぶ。create/updateも`get_available_tools(addon_name) or None`なので、利用可能toolが空なら制限を無効化する。
  - executorは指定名をglobal `TOOL_REGISTRY`から取得し、意図的に`__wrapped__`を選んでbuilding gateを外して実行する。
  - persona/Aspect/addon ownership/capabilityの中央認可はない。一般tool境界の欠落は[Spell / Tool / Playbook監査](2026-07-15_spell_tool_playbook_permission_audit.md)にも記録済みだが、ここは外部admin routeから直接到達する具体的入口である。
- 影響:
  - addon UIの「テスト」を使って別addon/builtinのfilesystem、network、device、Track mutator等を任意引数で起動できる。空allowlist addonでは保存actionからも同じ能力を持てる。
- 修正方針:
  - empty listと無制限を別型にし、test/create/load/executeの全段でowner addonのallowlistと中央capability authorizationを強制する。wrapperを外す特例を廃止し、明示的なadmin capability tokenを使う。
- 必要な回帰:
  - empty/nonempty allowlist、native/MCP/他addon、building内外、test/save/manual JSONのmatrix。

### [P1] Addon updateがcheckout後の失敗をrollbackせず、runtime再登録失敗も成功として返す

- 場所: `saiverse/addon_installer.py:557-625`, `api/routes/addon_catalog.py:147-175, 265-323, 377-407`
- 事実:
  - updateは新commitを現在directoryへcheckoutしてからmanifest検証・setupを実行するが、失敗時に旧commitへ戻すtransactionがない。installにあるclone directory rollback相当もupdateにはない。
  - `_try_register_addon()`はintegration/hook再登録例外を握ってlogする。runnerはその後manifestを返すためSSE finalは`ok: true`になる。
- 影響:
  - update失敗後もdiskは新code、DB/data/setupは途中、runtimeは旧integrationという混成状態になる。成功表示後も再起動前後で別versionが動く。
- 修正方針:
  - detached staging checkoutでmanifest/setupを検証し、data migrationもbackup/rollback可能なtransactionとして完了後にatomic swapする。runtime activation失敗を成功にせず旧runtime/diskへ戻す。
- 必要な回帰:
  - fetch、manifest、各setup step、runtime unregister/register、SSE切断の各失敗点で旧versionが一貫して稼働すること。

### [P1] MCP subprocess起動logに解決済みenv辞書を全量出力する

- 場所: `tools/mcp_config.py:80-208`, `tools/mcp_client.py:472-495`
- 事実:
  - MCP configは`${env.*}`、global/persona addon param、named-instance contextを実値へ展開する。
  - `_connect_stdio()`は展開後の`server_params.env`をINFOでそのままbackend logへ書く。API key、device token、OAuth tokenを含む値のredactionがない。
- 影響:
  - session logとそのbackup/support共有へcredentialが恒久保存される。persona単位tokenも他persona・operator contextから読める形になる。
- 修正方針:
  - envはkey名だけを記録し、値は一律redactする。構造化loggerにもsecret-aware serializerを置き、例外文字列・subprocess stderrのtoken出力も検査する。
- 必要な回帰:
  - env/addon/persona/instance placeholderのsecret markerがbackend/subprocess/error logのどこにも現れないこと。

### [P1] MCP tool callが任意例外で接続を張り直して同じ副作用を自動再実行する

- 場所: `tools/mcp_client.py:537-565`
- 事実:
  - `session.call_tool()`の例外種別を問わずdisconnect/connect後に同じtool名・argumentsをもう一度送る。
  - server側で副作用完了後にresponseだけ失われた場合を区別するidempotency key、operation status照会、tool schema上のsafe-to-retry属性がない。
- 影響:
  - device操作、投稿、ファイル生成、購入等のMCP toolが二重実行される。timeoutを長くしても「適用済みだが応答不明」の不確実性は解消しない。
- 修正方針:
  - defaultはpost-dispatch retry禁止。read-only/idempotent宣言済みtoolだけ再試行し、side-effect toolにはoperation IDとserver側dedupe/status確認を要求する。
- 必要な回帰:
  - serverが適用前/適用後/response途中で切断するfault injectionで副作用が最大1回になること。

### [P1] `/api/mcp/tool-call`がpersona・spell・building gateを迂回してhidden/admin toolを直接実行する

- 場所: `api/routes/mcp.py:128-181`, `tools/mcp_client.py:537-568`
- 事実:
  - endpoint自身がpersona-spell pipelineをbypassすると明記し、connectionから任意`tool_name`を直接呼ぶ。
  - `spell_visible`、building IDs、active persona、Aspect、addon enabled、admin roleを検査しない。API認証もない。
- 影響:
  - UI/LLMから隠した診断・GPIO・device管理toolまでLANから直接起動できる。表示制御が実行権限として機能しない。
- 修正方針:
  - production routerからdebug callを外すか、local authenticated admin + capability allowlistに限定する。最終callは通常toolと同じ中央認可へ通す。
- 必要な回帰:
  - hidden/building-restricted/disabled/persona-scoped/admin toolが未認可APIから実行されないこと。

### [P1] Discord adapterとmanagerのmessage contractが一致せず、全human/remote persona message処理が例外になる

- 場所: `discord_gateway/saiverse_adapter.py:25-33, 79-92`, `manager/gateway.py:60-105`
- 事実:
  - `DiscordMessage`が持つのは`author_discord_id`, `author_role`, `visitor`等で、`author_name`と`persona_id`は存在しない。
  - managerはhuman処理で`handle_user_input()`実行後に両不存在属性を読み、remote処理ではentry作成時から`message.persona_id`を読む。
  - このcontractを通すmanager-level testは存在しない。
- 影響:
  - human入力はローカルLLM/tool処理だけ起動した後にresponse/history/ack生成が失敗し、retryで副作用が増える。remote persona発話は受信できない。
- 修正方針:
  - author identityを型付きunion（human/registered visitor）に統一し、adapterからmanagerまで同じDTOを使う。side effect開始前にcontract validationし、end-to-end testを置く。
- 必要な回帰:
  - human/visitor/bot/欠落authorそれぞれのWebSocket eventからBuilding保存・reply・ackまでの結合test。

### [P1] Discord human messageをmapped Buildingではなくlocal userの現在地で実行し、履歴と応答先を分裂させる

- 場所: `manager/gateway.py:60-89`, `manager/runtime.py:386-469`
- 事実:
  - gatewayはmapped `context.building_id`を持つが、会話実行はbuilding引数のない`handle_user_input(message.content)`へ渡す。
  - `handle_user_input`はmanager stateの`user_current_building_id`を使う。その後gatewayはuser entryをmapped Buildingへ追加し、生成responseはmapped Discord channelへ送る。
- 影響:
  - channel Aの入力でBuilding Bのpersona/toolが動き、Aの履歴にはuser発言だけ、Discord AにはBの応答が出る。人格・Building・tool permission境界が一致しない。
- 修正方針:
  - command入口へ明示`building_id`とactor identityを必須化し、mapped locationで認可・永続化・Pulse・replyを一つのtransaction IDに束ねる。
- 必要な回帰:
  - local current Buildingとmapped Buildingが異なるケース、複数channel同時入力、quarantine/permissionのmatrix。

### [P1] Discord memory syncが無制限buffer・persona未照合・未正規化filenameを一つのtrusted transferとして扱う

- 場所: `discord_gateway/orchestrator.py:280-359`, `manager/gateway.py:108-288`
- 事実:
  - initiateの`total_size`/`total_chunks`に上限がなく、chunkを`bytearray`へ追加してから宣言値超過だけを見る。transfer deadline/idle cleanupもない。
  - chunk/complete時にeventのvisitorと保存stateの`persona_id`/`owner_user_id`が一致するか検査しない。
  - 完了blobのfilenameはremote由来の`persona_id`と`transfer_id`をそのまま連結し、resolve-containmentやbasename制約なしで`write_bytes()`する。
- 影響:
  - authenticated relay/clientの侵害またはprotocol不整合でprocess memoryを枯渇させ、personaを永久に`transfer_in_progress`へ固定できる。別visitorのtransferへchunkを混入でき、Windows drive/absolute/traversal形式では意図しない既存directoryへbinaryを書ける可能性がある。
- 修正方針:
  - total/chunk/count/timeを固定上限にし、bounded temp fileへ順序付きstream writeする。transfer stateにauthenticated session・persona・ownerをbindingし全eventで照合する。保存名はhost生成UUIDだけにする。
- 必要な回帰:
  - oversized、out-of-order、duplicate、cross-persona、timeout、drive/UNC/separator ID、checksum失敗のmatrix。

### [P1] Discord outgoing commandをsend前にqueueから除去し、接続断でmessage/memory chunkを無通知に失う

- 場所: `discord_gateway/gateway_service.py:116-124`, `manager/gateway.py:291-390`
- 事実:
  - senderは`outgoing_queue.get()`後に一度だけ`send_json()`し、失敗時のrequeue、durable outbox、delivery ackとの対応付けがない。
  - connection loopは例外で再接続するが、既にdequeueしたcommandを復元しない。persona messageとmemory sync initiate/chunk/completeが同じqueueを使う。
- 影響:
  - 短い切断でpersona発話またはmemory chunkが消える。送信済みか不明な場合のdedupeもなく、素朴なretryを追加すると今度は二重送信になる。
- 修正方針:
  - command ID付きdurable outbox、relay ack、再接続後resume、受信側dedupeを一組で実装する。memory transferは欠落chunkを再送要求できるprotocolにする。
- 必要な回帰:
  - dequeue前/後、send前/途中/後、ack前/後の切断でloss/duplicateがないこと。

### [P2] provider/router/structured-output診断がmodel raw responseを通常logへ複製する

- 場所: `saiverse/llm_router.py:215-227`, `llm_clients/openai.py:369-375`, `llm_clients/ollama.py:674-719, 760-803`
- 事実:
  - router JSON parse失敗はraw response、OpenAI empty structured outputはmessage全体、Ollama streamingは先頭5 raw chunkとlast chunkをWARNING/INFOへ出す。
  - 専用`llm_io.log`の明示的なI/O記録とは別にbackend logへ複製され、retention/redaction policyを分けられない。
- 影響:
  - user/persona本文、reasoning、tool argument、provider metadataが運用log・support bundleへ予期せず残る。
- 修正方針:
  - 通常logはrequest/correlation ID、provider、status、sizeだけにし、raw bodyは明示opt-inのsecret-aware I/O sinkへ限定する。

### [P2] routerのfree→paid fallback判定が例外文字列substringで、成功後はprocess全体をpaidへ固定する

- 場所: `saiverse/llm_router.py:34-40, 155-207`
- 事実:
  - `rate`, `quota`, `429`, `503`, `unavailable`, `overload`が例外文字列のどこかにあればpaid clientへretryする。
  - 一度成功するとglobal `client`をpaidへ置換し、free回復probeやrequest単位のbudget/consentなしに以後もpaidを使う。
- 影響:
  - 接続先・proxyの曖昧なerror文でも課金経路へ移り、短時間障害がprocess lifetimeの費用方針変更になる。
- 修正方針:
  - providerの型付きstatus/retry metadataで分類し、fallback policy・budget・cooldownを明示設定する。global固定ではなくcircuit breakerとして回復probeする。

### [P2] Addon catalogの供給元検証がHTTP許可・7桁SHA許可・署名なしに留まる

- 場所: `saiverse/addon_registry.py:60-77, 99-128, 199-224`, `saiverse/addon_installer.py:482-523`
- 事実:
  - registry entryのrepo URLは`http://`も許可し、commitは7〜40 hexを受理する。registry/addon manifest/packageの署名・publisher identity検証はない。
  - install直後にmanifest由来のpip/script/setupをhost権限で実行するため、catalogは実質code-signing境界である。
- 影響:
  - registry配信またはHTTP経路の侵害がそのままhost code executionへ到達する。短いobject IDはfull SHAより衝突・曖昧性耐性が低い。
- 修正方針:
  - HTTPS-only、full object ID、署名済みregistry/manifest、publisher key pin、表示上のtrust provenanceを要求する。

### [P2] MCP connect/initializeにdeadlineがなく、直列startupの一serverが後続を停止できる

- 場所: `tools/mcp_client.py:350-388, 646-706`
- 事実:
  - `connect()`は`_ready_future`をdeadlineなしで待ち、owner taskはtransport openと`session.initialize()`を期限なしで待つ。
  - `start_all()`はglobal serverを順番にawaitするため、応答しないserver一つで後続MCP初期化が進まない。
- 影響:
  - addon/MCP subprocessのhangが他の独立tool availabilityとstartup完了を巻き込む。
- 修正方針:
  - transport/initializeごとのdeadline、server単位supervisor、並列startup、失敗隔離を入れる。

### [P2] Discord gatewayが`ws://`を正式許可し、handshake tokenと全messageを平文送信できる

- 場所: `discord_gateway/config.py:34-42`, `discord_gateway/client.py:35-56`
- 事実:
  - URL validatorは`ws://`と`wss://`を同等に許可し、clientはTLS要件なしで最初のpayloadにtokenを入れる。
- 影響:
  - loopback外の`ws://`設定では同一network上からsession token、persona発話、memory blobを盗聴・改変できる。
- 修正方針:
  - defaultはWSS必須、`ws://`はloopbackか明示dangerous development flagだけに限定する。証明書/hostname検証を回帰固定する。

### [P2] OAuth redirect baseが未検証`X-Forwarded-*`を信頼する

- 場所: `api/routes/oauth.py:39-68`, `saiverse/oauth/handler.py`
- 事実:
  - start endpointはproxy trust設定を確認せず`X-Forwarded-Proto`と`X-Forwarded-Host`を優先し、そのbaseをcallback URL生成へ渡す。
  - trusted proxy/allowed public originの照合がない。
- 影響:
  - proxy構成や直接到達条件によっては認可codeのcallback先を攻撃者指定hostへ誘導するURLを生成できる。
- 修正方針:
  - redirect URIをserver設定のallowlistから選び、forwarded headerはtrusted proxy middlewareで正規化済みの場合だけ使う。

## Coverage

- LLM: provider/model secret解決、connection test、OpenAI/Anthropic/Ollama retry・streaming、Gemini router fallback、raw response logging。
- Addon: config/persona/OAuth secret、catalog registry、install/update/uninstall、runtime register、composite action CRUD/test/execute。
- MCP: config priority/placeholder、stdio/SSE/HTTP connection、startup/backoff、tool discovery/registration、call retry、instance/admin API、building gate。
- Discord: WebSocket auth/reconnect/queue、channel mapping/permission、visitor registry、human/remote message adapter、memory sync、history/reply送信。
- 未確認: 各third-party addon内部実装と各remote provider/server側のauthorization・retentionは、このcore監査のscope外。導入addonごとに別途owner監査が必要。

## 集計

- **P1×12 / P2×6**
- 一次監査は完了。これによりコードレビュー台帳の全P2行まで一次監査が完了した。以後は修正追跡と回帰固定へ移る。

## 修正追跡（2026-07-16・第一陣）

- **修正・回帰固定 [P1×1]**: MCP subprocess起動logから引数値とenv値を除き、`arg_count`と`env_keys`だけを記録する。secret marker非出力を回帰化した。
- **修正・回帰固定 [P2×1]**: router/OpenAI/Ollamaの通常logからraw response、structured candidate/message、stream chunk本文を除き、件数・文字数・statusだけを残す。
- **部分修正 [P2×1]**: MCP `connect()`へ既定30秒・server設定可能な`startup_timeout`を入れ、超過時はowner task自身のcontext巻き戻しを保って中断する。直列startupの無期限停止は解消したが、server並列startup/supervisor化は未実装のためfinding全体は部分修正扱いとする。
- **明示保留**: Discord関連は、現行導線が実運用されておらず抜本改修で置換される可能性が高いというまはー判断により、第一陣ではコード変更も修正済み判定も行っていない。

## 修正追跡（2026-07-16・第二陣）

- Tool/Playbook/MCP/Addon actionは中央実行時認可へ集約し、Addon有効状態、Aspect spell権限、allowlistを実行直前に再検査する。
- Anthropic/Ollama streamingは最初の出力をcommit pointとし、その後はretry/fallbackしない。MCP callの結果不明例外も自動reconnect＋再送しない。
- routerの例外文字列substring free→paid判定とprocess-global paid固定を撤去した。将来のmodel単位任意fallback chainは `model_provider_management.md` に設計だけを残した。
- custom provider/modelのcredential envとbase URLを束縛し、private/non-global hostは明示allowlist、HTTPはloopback/明示hostだけを許可した。
- Addon registry/manifestはHTTPSとfull lowercase SHAを要求する。公式registryはEd25519署名＋固定public key必須、unsigned third-party remoteは明示opt-in必須とした。公式鍵と署名済みregistryのpublishは運用側必須作業として残る。
- Discordはまはー判断どおり第二陣でも対象外。

回帰: `tests/test_external_retry_commit_point.py`、`tests/test_tool_execute_authorization.py`、`tests/test_addon_registry_trust.py`で固定した。
