# API / frontend 一次監査

**開始・完了日**: 2026-07-15  
**状態**: 指摘あり・一次監査完了  
**監査基準**: `113567e`  
**監査軸**: backend境界、破壊操作、file/upload、chat冪等性、SSE、frontendの失敗表示・楽観更新・並行更新

## Findings

### [P1] `0.0.0.0`公開backendに認証なしの全DB read/write/delete・環境変数更新・restart/update APIがある

- 場所: `main.py:510-576`, `api/main.py:12-35`, `api/routes/db_manager.py:18-134`, `api/routes/admin.py:20-168`, `api/routes/system.py:340-454`
- 事実:
  - backendは全interfaceへbindし、CORSは`allow_origins=["*"]`、全method/header許可である。Observer以外の通常APIに認証middlewareはない。
  - `/api/db/tables/{table}`は全ORM modelの全columnを返し、任意rowを`merge()`またはdeleteできる。allowlistはtable名だけで、persona/city/field scopeはない。
  - delete requestの`pks`が空、または全keyが不正でもfilterが一つも付かず、`query.first()`のrowを削除する。
  - `/api/admin/env`はsensitive flagを付けるだけで`.env`の実値を返し、POSTで任意keyをruntimeとfileへ書ける。restartとself-updateも認証なしで起動できる。
  - DBにはpersona記憶・schedule・world state・addonの`params_json`（OAuth access/refresh tokenを含み得る）がある。
- 影響:
  - 同一LAN上の任意host、またはブラウザからbackendへ到達できる任意originが秘密情報を取得し、人格・記憶・権限・worldを改変/削除し、serverを停止・更新できる。
- 修正方針:
  - default bindをloopbackにし、LAN公開は明示設定にする。全APIへsession authentication、CSRF/origin policy、role/capability authorizationを置く。
  - generic DB managerはproduction routerから外し、必要なadmin操作を型付きcommandへ限定する。少なくとも空PK deleteを400で拒否する。
  - secretはGETで返さず、設定済み状態と末尾maskだけを返す。
- 必要な回帰:
  - unauthenticated/cross-origin/LAN requestの拒否、role別read/write、empty/unknown PK delete拒否、secret非返却。

### [P1] Item/media/avatar/file APIが保存pathとfilenameをcontainment検証せず、任意local fileを読取・上書きできる

- 場所: `api/routes/info.py:450-588, 622-679`, `api/routes/media.py:319-369`, `api/routes/chat.py:65-83`, `api/routes/world.py:488-536`
- 事実:
  - world itemの`file_path`はAPI入力から保存でき、`/api/info/item/{id}`はabsolute pathならそのまま`read_text()`/`FileResponse()`する。document content PUTは同じpathへ`write_text()`する。
  - pathが`saiverse_home`配下か、item typeに対応するdirectoryかをresolve後に検査しない。
  - media serveは`dest_dir / filename`だけで、Windowsのbackslash traversalを含むfilenameをreject/resolve-containmentしない。
  - persona avatarもDBのpathをそのまま`FileResponse`する。
- 影響:
  - APIだけで`.env`、DB、source、ユーザーファイル等を読め、text fileは上書きできる。unauthenticated API境界と組み合わさるとLAN越しに成立する。
- 修正方針:
  - DBにはopaque media/item IDまたは正規化relative pathだけを保存し、serve/edit時に`resolve()`後の`is_relative_to(allowlisted_root)`を強制する。
  - route filenameはbasename一致を要求し、backslash、drive、`..`、separator、ADSを拒否する。
- 必要な回帰:
  - absolute path、`../`、URL-encoded slash/backslash、Windows drive/UNC、symlink escape、正常mediaのmatrix。

### [P1] user messageの永続化失敗を握り潰してLLM処理を続け、生ログなしの応答・副作用を生成する

- 場所: `manager/runtime.py:397-469, 476-623`, `database/building_messages.py:398-488`
- 事実:
  - streaming/non-streamingとも`add_to_building_only()`例外をlogするだけでpersona dispatchを続ける。
  - DB helperはlock retry後の失敗をraiseせず`None`で返す。streaming callerは`bm is None`も失敗扱いせず続行する。
  - responding personaが0人ならuser message自体を保存しないが、API streamは成功する。
- 影響:
  - LLM応答・tool副作用・課金は発生するのに、原因となったユーザー原文がBuilding logに存在しない。UIの楽観表示だけが残り、再読込・Metabolism・監査で消える。
- 修正方針:
  - user utteranceのdurable insertをPulse起動の必須preconditionにし、確定message IDが得られなければdispatchしない。empty roomでもuser発言は保存する。
  - persistence errorを構造化terminal eventで返し、retry可能性を明示する。
- 必要な回帰:
  - insertがexception/None/lock exhaustion、empty room、stream切断でも、未保存入力からPulseが起動しないこと。

### [P1] `client_message_id`がuser rowだけを重複排除し、retryのたびにLLM・tool・返答を再実行する

- 場所: `database/building_messages.py:417-459`, `manager/runtime.py:547-605`, `frontend/src/app/page.tsx:1582-1631`
- 事実:
  - duplicate IDではDB helperが既存user rowを返すが、duplicateか新規かをcallerへ示さない。
  - runtimeは返却後、常に全responding personaをdispatchする。同一request retryでuser rowは一件でも、assistant応答・Playbook・spell・外部toolは複数回走る。
  - frontendは送信一回ごとにUUIDを作るが、失敗後の再送操作では新UUIDになり、元requestの再開/結果取得もない。
- 影響:
  - network retryやmulti-device再送で二重投稿、二重課金、二重外部副作用が発生する。「idempotency」の表示と実際のcommand semanticsが一致しない。
- 修正方針:
  - idempotency keyをutter command全体へ適用し、processing/completed/failedとresponse stream/resultを永続化する。同じkeyは既存結果へattachし、Pulseを再起動しない。
- 必要な回帰:
  - insert前/後、Pulse中、response後の切断とretryでtool call・assistant responseが一度だけになること。

### [P1] uploadのsize制限を全量`read()`後に判定し、image/documentは上限自体がない

- 場所: `api/routes/media.py:32-318`, `api/routes/addon.py:754-805`
- 事実:
  - image、hires image、documentは`await file.read()`で全量をmemoryへ読み、入力byte上限がない。
  - audio/video/addon fileは上限を持つが、同じく全量read後に`len(content)`を確認するためmemory exhaustion防止にならない。
  - backendは認証なしで全interface公開される。
- 影響:
  - 少数の巨大multipart requestでprocess memory、画像decoder、diskを枯渇させられる。
- 修正方針:
  - reverse proxy/ASGIとendpoint双方でContent-Length・streaming byte countを先に制限し、bounded temp fileへchunk書込する。画像はdecode前にpixel/dimension bombも検査する。
- 必要な回帰:
  - Content-Lengthあり/なし、chunked、宣言MIME偽装、pixel bomb、limit直前/超過でmemoryが上限内に収まること。

### [P2] chat失敗時も楽観user messageを残し、入力・添付・Playbook引数を復元しない

- 場所: `frontend/src/app/page.tsx:1511-1668, 2075-2110`
- 事実:
  - fetch前にuser messageを追加し、input、attachments、playbook argsをclearする。
  - non-2xx/stream error時は汎用assistant errorを追加するだけで、temp user messageをfailed表示・除去せず、入力類も戻さない。
  - finallyのsync後もserverに無い楽観messageがUIに残り得る。
- 影響:
  - ユーザーには送信済みに見えるが、再読込で原文/添付が消える。再送には内容の再入力が必要になる。
- 修正方針:
  - optimistic rowを`pending/confirmed/failed`で管理し、失敗時は同じidempotency keyでretry可能にしてdraft/attachmentを保持する。

### [P2] 複数の破壊・toggle UIがHTTP statusを確認せず、失敗を成功後の再読込として扱う

- 場所: `frontend/src/components/ScheduleModal.tsx:309-325`, `frontend/src/components/ActionsPanel.tsx:153-161`, `frontend/src/components/AddonManagerModal.tsx:438-446`
- 事実:
  - schedule toggle/delete、action delete、addon file delete等が`await fetch()`後に`res.ok`を確認せずreload/closeする。fetchは4xx/5xxでrejectしない。
  - error bodyを表示せずconsoleだけ、または完全にignoreする経路がある。
- 影響:
  - 権限拒否・DB失敗・validation errorをユーザーが認識できず、画面再読込まで実状態が不明になる。
- 修正方針:
  - 共通typed API clientでnon-2xxを必ずthrowし、mutation中/失敗/再試行を各UIへ表示する。

### [P2] backendが内部exception文字列を多数の500 responseとSSE technical detailへそのまま返す

- 場所: `api/routes/world.py`, `api/routes/info.py`, `api/routes/db_manager.py`, `api/routes/chat.py`, `manager/runtime.py:606-621`
- 事実:
  - `detail=str(e)`や`technical_detail=str(e)`が広く使われ、SQL error、absolute path、provider応答、内部class名をclientへ返す。
  - frontendはerror detailを展開表示する。
- 影響:
  - filesystem/DB/schema/provider情報が漏れ、外部入力からの探索を助ける。ユーザー向け文言も実装依存になる。
- 修正方針:
  - public error code/messageとserver-side correlation IDへ分け、technical detailはlogだけに残す。DEBUG表示もlocal authenticated adminに限定する。

### [P2] admin/configのfull-resource更新にrevision/CASがなく、複数clientの保存が相互に上書きされる

- 場所: `api/routes/world.py:21-56, 240-248, 422-426`, `manager/admin.py:444-507`, `frontend/src/components/BuildingSettingsModal.tsx:190-213`
- 事実:
  - Building、AI、City、各種configはGET時revision/ETagを返さず、PUTはclientが保持する全fieldを最後書込勝ちで保存する。
  - UIはmodal open時のsnapshotを送るため、別clientが変更したfieldを古い値へ戻してもconflictにならない。
- 影響:
  - multi-device運用でtool links、prompt、model、toggle等の変更が静かに失われる。
- 修正方針:
  - entityへrevision/updated_atを持たせ、`If-Match`またはbody versionのCAS updateにする。単一fieldはPATCH commandへ分離する。

## Coverage

- FastAPI bind/CORS/router/auth、generic DB/admin/system/world/config/people/chat/info/media/addon API。
- file serving/upload、generic CRUD、破壊操作、SSE/NDJSON error、chat persistence/idempotency。
- frontend main chat、schedule、addon/action、settings/world editor、memory UIの主要mutation/error handling。
- 未確認: 各addon独自frontend/API routerの契約はowner addonごとの追加監査が必要。

## 集計

- **P1×5 / P2×4**
- 一次監査は完了。以後は修正追跡と回帰固定へ移る。

## 修正追跡（2026-07-16・第一陣）

- **修正 [P2×1]**: schedule toggle/delete、composite action delete、addon file delete、addon enabled toggle、global/persona addon config保存で`Response.ok`を必須判定にした。4xx/5xx時はreload・close・親state更新へ進まず、応答detailまたはstatusをUIへ表示する。
- frontend対象3fileはTypeScript `--noEmit`成功、ESLint error 0を確認した（既存warning 21件は本finding外）。

## 修正追跡（2026-07-16・第二陣）

- backend既定bindをloopbackにし、非loopbackではowner tokenと許可Originを起動必須条件にした。BearerまたはHttpOnly/SameSite owner sessionを要求し、cookie mutationはOrigin照合する。
- admin/addon/OAuth secretは実値を応答せずmask＋`is_set`にした。mask/空値の再保存は既存secretを消さない。
- persisted file pathはSAIVerse managed rootsと明示外部mountだけに制限し、repo直下 `.env` 等は許可しない。filename traversalとsymlink解決後のroot escapeを拒否する。
- image/document/native/chatlog/Addon file uploadへhard limitを入れ、大容量audio/video/chatlog/Addon fileはbounded streamingでtempへ書く。413を500へ潰さずpartial fileを除去する。
- generic DB DELETEは全主キー完全一致を要求し、空条件・unknown/partial keyを拒否する。
- user utteranceはPulseより先に永続化し、canonical message idを返す。同じ `client_message_id` の再送はLLM/toolを再実行しない。

回帰: `tests/test_owner_auth.py`、`tests/test_api_file_boundaries.py`、`tests/test_user_utterance_durability.py`ほか既存API suiteで固定した。
