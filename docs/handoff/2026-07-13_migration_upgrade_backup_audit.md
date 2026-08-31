# migration / upgrade / backup 一次監査

**開始日**: 2026-07-13

**状態**: 指摘あり・一次監査完了（2026-07-13）
**監査軸**: 変更前復元点 / 失敗伝播 / 冪等性 / 部分適用 / updater parity / 復元原子性

## 一次監査coverage

確認済み:

- `main.py` 起動時のschema migration → data backfill → version-aware upgrade → startup backupの順序
- `database/migrate.py` のadditive pathとfull rewrite path、rollback・CLI終了状態
- `saiverse/upgrade.py` のhandler選択、entity version更新、失敗時rollback
- 現行upgrade handler（v0.3.0.dev0〜dev4）の失敗・冪等性方針
- `update.bat` / `update.sh` / `scripts/self_update.py` の更新フェーズとエラー処理
- World Editor legacy `backup_world()` / `restore_world()` と独立`scripts/snapshot.py`の役割重複
- main DB backupのWAL内容・connection lifecycle・命名衝突・retention選別・復元導線
- snapshotの保存失敗、上書きpublish、起動中検出、metadata/member検証、破壊的復元・rollback
- updater三経路のZIP/git差分、削除file、phase停止条件、process ownership、起動context、再起動確認・rollback境界
- 現行post-migration/data-only hookの失敗伝播と旧列データ写像
- version-aware handlerのregistry連続性、未来version、複数City/AIの部分完了・再起動、City間移動version境界

一次監査はここで完了とし、以後は修正状況と回帰テストを追跡する。

## Findings

### [P1] 起動時backupがmigration・data backfill・upgrade handlerより後で、変更前の自動復元点がない

- 場所: `main.py:316-379`, `database/backup.py:31-132`, `docs/intent/version_aware_world_and_persona.md:178-184,246-251`
- 事実: 起動順はschema migration（additive/full rewrite）→無条件data backfill群→version-aware handler→background startup backup。full rewriteだけは内部で元DBをrenameして`.bak`を残すが、additive ALTER、day-plan/desire/note backfill、upgrade handlerには変更前backupがない。最後のstartup backupは変更後状態を保存する。
- 影響: 追加系migrationまたはデータhandlerが論理的に誤っていても、通常のstartup backupは誤適用後しか残さない。Intentの「復元なしの実機テストはしない」「不可逆処理前に復元点を持つ」と実行順が一致しない。
- 修正方針: DBを開いて変更する前にSQLite backup APIでpre-upgrade snapshotを同期作成し、成功確認後にmigrationへ進む。通常startup backupとは名前・retention・用途を分け、対象code versionと旧DB versionをmetadataへ刻む。
- 必要な回帰: additive、data-only handler、version-aware handlerをそれぞれ途中失敗させ、変更前backupから元状態を復元できること。backup作成失敗時はmigrationを開始しないこと。

### [P1] full rewrite migrationが失敗をrollback後に握り潰し、CLIと起動側が成功として続行する

- 場所: `database/migrate.py:203-367,895-912`, `main.py:321-330`, `update.bat:126-136`, `update.sh:130-142`, `scripts/self_update.py:350-355`
- 事実: `migrate_database_in_place()`は本処理例外をcatchしてrollbackを試みるが、その後raiseも失敗returnも行わない。rollback自体の例外もログだけで握り潰す。CLI mainは戻り値を検査せず正常終了する。`main.py`も直後に「Database migration completed」と記録して後段へ進む。
- 最小再現: 一時DBで`Base.metadata.create_all()`を強制例外化した。元DBへのrollbackは成功したが、関数は例外を出さず`None`を返した。したがってCLI終了コードは0相当になる。
- 影響: rollback成功時でも新code＋旧schemaのままupgrade/startupへ進む。rollback失敗時は部分DBの可能性をログに出すだけで同様に続行する。updaterもDBフェーズをOKと誤表示し得る。
- 修正方針: migration失敗はrollback結果にかかわらず専用例外をraiseする。rollbackにも失敗した場合は元DB/backup/partial targetの全pathを明示したfatal errorにする。CLIは非0、`main.py`は起動中断、updaterは後続playbook import/restartを停止する。
- 必要な回帰: table copy、post-hook、rollback file moveの各失敗で非0終了し、呼び出し側が成功ログ・後続フェーズを実行しないこと。

### [P1] upgrade handlerが壊れた対象をskipしてもentity versionを最終版へ進め、再試行不能にする

- 場所: `saiverse/upgrade.py:150-215`, `saiverse/upgrade_handlers.py:26-94,180-270`
- 事実: 基盤はhandlerが例外を上げない限り最後に`LAST_KNOWN_VERSION=target`をcommitする。一方dynamic-state handlerはmalformed `LAST_NOTIFIED_JSON`を警告してskipし、legacy schedule selected-playbook handlerもmalformed `PLAYBOOK_PARAMS`を警告してskipする。いずれも未移行行を残したまま成功扱いになる。
- 影響: 次回起動では`current >= target`で全handlerがno-opになり、壊れた行は修正しても自動再移行されない。「失敗時は例外を上げ、versionを進めない」というIntent不変条件を破る。
- 修正方針: handler結果にprocessed/skipped/errorを持たせ、移行対象のparse失敗はentity upgrade失敗として例外化する。ユーザー修復が必要な破損は対象ID・raw値・復旧手順をログへ残し、versionを据え置く。
- 必要な回帰: malformed JSONを含むAIでstartup upgradeがFalseになり、versionが旧値のまま、修復後の再起動で同じhandlerが完走すること。

### [P1] external memory副作用を持つhandlerが冪等でなく、後続失敗時の再実行で通知を重複する

- 場所: `saiverse/upgrade.py:178-207`, `saiverse/upgrade_handlers.py:26-147`
- 事実: v0.3.0 dynamic-state handlerはmain DB session外の`SAIMemoryAdapter`でupgrade通知を即時commitする。通知にはhandler/entityを一意にするidempotency keyがない。後続handlerまたは最終version commitが失敗するとAI versionは旧値のままなので、次回起動でdynamic-state handlerが再実行され通知が累積する。handler docstringも強制複数実行で累積する事実を認めるが、基盤の失敗再実行経路を考慮していない。
- 影響: 一度のupgradeが本人のmemory.dbでは複数のシステム経験として刻まれる。main DB rollbackで外部DB副作用は戻らず、versionと実行済み効果が分離する。
- 修正方針: `(handler_name, persona_id, target_version)`の実行台帳またはmessage idempotency keyをmemory.db側へ持たせ、同じ通知を一度だけ保存する。cross-DB handlerはprepare/commit状態を永続化し、再実行で収束させる。
- 必要な回帰: 通知保存後に後続handlerを失敗させ、再起動を複数回行っても通知が1件だけで、修復後versionが進むこと。

### [P1] World Editorのrestoreが稼働中DBとpersona treeを先に削除し、検証・rollbackなしで部分復元する

- 場所: `manager/history.py:201-319`
- 事実: `restore_world()`はSAIVerseManagerの稼働中メソッドであり、停止確認もDB connection closeも行わず、現DB・cities・personas・buildingsを先に削除する。その後ZIPをtempへ展開して各対象を個別moveする。archive整合性・必須DB・SQLite integrityの事前検証、自動pre-restore backup、失敗時rollbackはない。
- 影響: Windowsではopen DB削除で途中失敗し、Unix系ではunlink後も既存connectionが旧inodeへ書き続けて復元DBとsplit-brainになる可能性がある。展開/move途中の失敗は人格treeとmain DBの世代が異なる部分状態を残す。
- 修正方針: legacy World Editor restoreを停止状態専用`scripts/snapshot.py`へ一本化する。全展開・hash/manifest/SQLite integrity検証→全connection停止→tree swap→失敗時自動rollbackの順にする。
- 必要な回帰: 稼働中restore拒否、壊れたZIP、DB欠落、persona move失敗で現状態が不変であること。成功時にmain DBと全persona DBが同じsnapshot世代へ切り替わること。

### [P1] updaterが重要フェーズ失敗後も混在状態を作り、3実装の停止条件も一致しない

- 場所: `update.bat`, `update.sh`, `scripts/self_update.py:327-372,480-505`, `CLAUDE.md`「Setup/Update Script Parity」
- 事実:
  - bat/shはpip失敗で停止するが、DB migration・playbook import・npm失敗はWARNで続行する。
  - self updaterの`_run()`はpipを含む全失敗をwarningだけにし、戻り値を上位へ返さない。code update失敗時もdependency updateを実行し、最後は必ずrestartして「Self-update complete」と記録する。
  - git stashもbat/shは保存したまま手動pop、self updaterは自動popし、conflict時にworking treeを`reset --hard HEAD`する。規約上同一であるべき3実装のerror/stash意味論が一致しない。
- 影響: 新code＋旧dependency、旧schema＋新playbook、backend更新済み＋frontend旧版などの混在状態で自動再起動する。summary/完了ログが実際の適用状態と一致しない。
- 修正方針: 全実装で共通phase contractを定義し、code/dependency/migration/playbook/frontendの成否を構造化して集約する。互換性に必須なphase失敗では後続mutationとrestartを停止し、旧codeでの安全な再起動可否を明示する。stash policyも一本化する。
- 必要な回帰: 各phaseを1つずつ失敗させ、3実装が同じ停止点・終了コード・summary・stash状態になること。migration失敗後にplaybook importとrestartが走らないこと。

## Findings（第2片: backup実体 / post-hook / ZIP update）

### [P1] full rewriteのpost-migration hookが移行不能データをskipし、旧列を捨てたtargetを成功扱いにする

- 場所: `database/migrate.py:329-352,369-530,533-594,606-636,860-888`
- 事実:
  - full rewriteは共通カラムだけを新DBへcopyした後、削除された旧列の意味をpost-hookで新構造へ写す。`INTERACTION_MODE`、`action_track.tasks_json`がこの方式である。
  - しかし各hookは外側例外をwarningで握り潰す。`tasks_json`は行単位のJSON parse失敗もwarning＋continueし、hook全体は「0 Track / 0タスクを移行完了」と記録する。
  - 最小再現: sourceの`t1.tasks_json='{broken'`と空target `persona_task`でhookを実行した。例外は返らず、target行数0、完了INFOが出た。full rewrite本体ではtarget `action_track`に`tasks_json`列が無いため、このままmigration成功となる。
- 影響: 旧DB backupにはデータが残るが、稼働を続ける新DBからはchecklistが欠落する。version/schemaは新状態なので自動再試行されず、warningを見落とすと失われたこと自体を検知できない。
- 修正方針: 旧列を落とすために必須なpost-hookはstrictにし、1行でも変換不能ならfull rewrite全体を失敗・rollbackする。意図的に隔離するなら、raw旧値とentity IDをquarantine tableへ保存し、未解決件数0をmigration成功条件にする。
- 必要な回帰: malformed JSON、target INSERT失敗、interaction state更新失敗で元DBへrollbackし非0終了すること。全source対象数＝移行済み＋明示quarantine数を検算すること。

### [P1] ZIP updaterがsourceに存在しないtracked fileを削除せず、旧コードと新コードを合成する

- 場所: `scripts/update_from_github.ps1:67-91`, `update.sh:84-97`, `scripts/self_update.py:263-315`
- 事実: GitHub ZIP経路は全実装ともsourceからdestinationへのcopy/`rsync -a`のみで、保護対象を除いたmirror deleteを行わない。新versionで削除されたdestination fileを列挙・退役させるmanifestもない。
- 最小再現: destinationに`current.py`と`retired.py`、ZIPに新しい`current.py`だけを置いて`update_via_zip()`を実行した。成功`True`、currentは更新されたが`retired.py`は残った。
- 影響: 削除済みPython module、tool、phenomenon、playbook JSON、frontend assetが旧版のまま残る。SAIVerseはdirectory autodiscoveryが多いため、単なるごみではなく退役機能が再登録され、新codeと同時稼働し得る。copy途中失敗でも既に上書きしたfileは戻らず、前片の「失敗後もrestart」と合流する。
- 修正方針: 配布manifestを持ち、保護rootを除くtracked treeをstagingへ構築して原子的swapする。少なくとも前回manifestとの差分で削除対象を限定し、ユーザー生成・gitignore対象には触れない。三つのZIP実装を共通Python engineへ一本化する。
- 必要な回帰: tracked file/dirの削除・rename、protected user file、copy途中失敗で、Git更新とZIP更新の最終treeが同一になること。

### [P2] main DB backupがSQLite connectionを明示closeせず、完了後もWindows file handleを残す

- 場所: `database/backup.py:57-80`
- 事実: `with sqlite3.connect(...) as conn`はtransactionをcommit/rollbackするcontext managerであり、connectionをcloseしない。コードコメントは「Ensure backup is cleanly closed」としているが`closing()`も明示`close()`もない。
- 最小再現: WAL modeのDBへ50行commitした状態で`backup_saiverse_db()`を実行。backup単体は50行・`PRAGMA integrity_check=ok`だった一方、writerをcloseした後もWindowsが元DBを使用中としてtemp directory削除を`WinError 32`で拒否した。
- 影響: startup backup完了直後のfile move/delete/restore、テスト環境cleanupを不定に阻害する。GC時期依存なので再現が操作順に左右され、full rewriteやWorld restoreのfile操作失敗要因になる。
- 修正方針: SAIMemory backupと同じ`contextlib.closing(sqlite3.connect(...))`をsource/destination双方に使い、関数return前にhandle解放を保証する。必要ならWindowsでrename/delete probeを回帰に含める。
- 必要な回帰: WAL backup直後に元DBとbackup DBをrename/deleteでき、backup内容とintegrityが保たれること。

### backup正常系で確認済み（新規findingなし）

- SQLite backup APIは、未checkpointのWALが存在する状態でもcommit済み50行をbackupへ取り込み、元writer close後にbackup単体で`COUNT(*)=50`、`PRAGMA integrity_check='ok'`を確認できた。データsnapshot方式自体は妥当で、問題は実行順（第1片）とconnection lifecycle（上記P2）。

## Findings（第3片: version逆行 / 複数City / legacy backup / data-only backfill）

### [P1] DBの記録versionが実行codeより新しくても正常扱いし、古いcodeを新しい永続状態へ接続する

- 場所: `saiverse/upgrade.py:149-215`
- 事実: `_run_handlers_for_entity()`は`current >= target`を一括して「upgrade不要」と判定する。等しい場合だけでなく、DB側が新しい場合もwarning・起動中断・downgrade処理なしで`True`を返す。
- 最小再現: `LAST_KNOWN_VERSION='9.0.0'`のentityへtarget `1.0.0`を指定した。結果は`True`、versionは`9.0.0`のまま、commit 0回だった。
- 影響: updater rollback、古いcheckout、複数installationの取り違えで、旧ORM・旧runtimeが新schema／新しい意味へ接続する。version-aware基盤が不可逆変更の境界を守らず、未知の未来versionを「処理済み」と誤認する。
- 修正方針: `current == target`だけをno-opにし、`current > target`は明示的なdowngrade/compatibility宣言がない限りfatal errorにする。対象City/AI、DB version、code version、復元手順をログへ残す。
- 必要な回帰: equalはno-op、olderは順次upgrade、newerはstartup全体を非0終了させ、entityもDBも変更しないこと。

### [P1] City間移動profileにversionがなく、暫定ルールの「version違い来訪拒否」を判定できない

- 場所: `docs/intent/version_aware_world_and_persona.md:113-132`, `manager/visitors.py:43-50,192-354`, `database/api_server.py:24-84`
- 事実: Intentは「シティとバージョンが違うペルソナは、他シティからの来訪を拒否する」と定める。しかしdispatch profileと`VisitingPersonaProfile`にはpersona/city versionがなく、API queueと`place_visiting_persona()`にもversion比較がない。受信側は差異を観測すらできない。
- 影響: versionがずれた複数Cityが動いた場合、古いstate/protocolのRemotePersonaProxyを新しいCityが受理する。来訪・思考・帰還のどの地点で互換性が崩れたかを識別できず、Intentが避けようとした未定義の補正問題へそのまま入る。
- 修正方針: dispatch payloadへ`source_city_version`と`persona_version`を必須で載せ、受信Cityのversionと完全一致しなければqueue投入前に同期的に拒否する。将来range互換へ広げる場合も、まずprotocol versionを明示する。
- 必要な回帰: 同一versionだけacceptし、source City差異、persona差異、version欠落、不正versionをHTTP応答時点でrejectすること。拒否requestをarrival queueへ残さないこと。

### [P1] legacy `backup_world()`が稼働中WALを含めず、commit済みデータ欠落ZIPを成功扱いする

- 場所: `manager/history.py:201-249`
- 事実: World Editorの`backup_world()`は停止確認もSQLite backup APIも使わず、main DBと各City/persona DBを`shutil.copy()`で順番にコピーする。`-wal`/`-shm`は対象外で、複数DBの同一時点性もない。
- 最小再現: WAL modeでschemaをcheckpoint後、20行をcommitしてWALに保持したまま`backup_world()`を実行した。元DBは20行、WAL 4152 bytes、関数は成功messageを返したが、ZIP内DBを単体で開くと0行だった。
- 影響: ユーザーには正常backupに見えるのに、復元時に直近のcommit済みworld stateが失われる。City/persona DBも別時刻にcopyされるため、main DB参照と各persona実体が異なる世代になる可能性がある。
- 修正方針: 稼働中経路を廃止して停止状態専用`scripts/snapshot.py`へ一本化するか、全SQLite接続を静止させて各DBをSQLite backup APIで取得する。archive作成後に各DBの`integrity_check`と世代manifestを検証してから成功を返す。
- 必要な回帰: 未checkpoint WALを持つmain/City/persona DBのcommit済み行が全て含まれ、各DBの世代tokenが一致すること。稼働中で静止できない場合はbackupを拒否すること。

### [P1] 必須data-only migrationが失敗をwarningで握り潰し、未正規化stateのままstartupを続行する

- 場所: `main.py:320-342`, `database/migrate.py:701-809`
- 事実: desire正規化は`stage`刻印、候補帳簿作成、旧note親解除、desire note削除を一transactionで行うが、内部の全例外をcatchしてwarningだけで返す。public wrapperは成否を返さず、`main.py`も無条件に次のmigration・upgrade・manager起動へ進む。
- 影響: transaction rollback自体は効くが、runtimeが前提とする`stage`と候補帳簿の不変条件が成立しないまま稼働する。次回起動で再試行はされるものの、その間の読み書きが旧表現と新codeの混在状態に対して行われる。
- 修正方針: runtime不変条件を作るdata-only migrationは例外を上げ、startupを中断する。cleanupだけのbest-effort hookとはAPIを分け、必須hookは対象件数・更新件数・残存不正件数を検算する。
- 必要な回帰: SQL失敗時に全stepがrollbackしstartupが中断すること。再起動後の成功時に全不変条件を満たしてからversion-aware handlerへ進むこと。

### [P2] startup `.bak`に列挙関数はあるが、検証・復元するsupported entrypointがない

- 場所: `database/backup.py:31-157`, `main.py:372-379`, `scripts/snapshot.py`
- 事実: startup backupはmain DB単体の`saiverse.db_backup_*.bak`を作り、`get_recent_backups()`も定義するが、repository内に同関数のcallerはない。この`.bak`をinspect、integrity検証、現DB退避、restoreするCLI/APIもない。独立`scripts/snapshot.py`は`~/.saiverse`全体の別形式ZIPを扱い、startup `.bak`は対象にしない。
- 影響: 第1片のpre-upgrade化を行っても、障害時の標準復旧は手動file置換に依存する。対象backupの選択ミス、稼働中置換、現状退避漏れを仕組みで防げず、保存された復旧点が運用上使えない可能性がある。
- 修正方針: `.bak`専用のlist/inspect/restore entrypointを設け、停止確認、`integrity_check`、現DB自動退避、原子的swap、復元後version表示まで一連で行う。あるいはstartup backup自体をsnapshot subsystemへ統合する。
- 必要な回帰: 最新/指定backupの列挙、破損backup拒否、稼働中拒否、復元前自動退避、swap途中失敗のrollback、復元後のversion/integrity確認。

### 複数entity upgradeの途中失敗・再開で確認済み（新規findingなし）

- City/AIごとにhandler transactionと`LAST_KNOWN_VERSION` commitが分かれているため、後続entityが失敗してstartupが中断しても、完了済みentityは次回`current == target`でskipされ、失敗entityだけが再試行される。この部分完了方式はIntentの「どこで止まったか追える」「部分復旧可能」と一致する。ただし、cross-DB副作用handlerの再実行問題とhandler内部skip問題は第1片の既出findingのままである。

## Findings（第4片: 独立snapshot安全装置）

### [P1] snapshot保存がfile単位の失敗を許容し、不完全archiveを終了コード0で成功扱いする

- 場所: `scripts/snapshot.py:270-336`
- 事実: `cmd_save()`は各`ZipFile.write()`の例外を`failed`へ積んで処理を続け、metadataには収集時の元`file_count`を記録する。失敗件数があってもtmp ZIPを正式名へrenameし、最後は`return 0`する。archiveのmember数・CRC・SQLite integrityは成功条件に含まれない。
- 最小再現: 2fileを収集し、片方の`write()`だけを強制失敗させた。出力は`OK: Snapshot saved`、終了コード0、metadata `file_count=2`だったが、archive内の実data fileは1件だけだった。
- 影響: updaterやユーザーは成功終了を信頼して不可逆migrationへ進めるが、必要file/DBが欠けた復旧点しか残らない。warningを見逃した場合、復元時まで欠落が発覚しない。
- 修正方針: member追加が1件でも失敗したらtmp archiveを破棄して非0終了する。完成後にmanifest件数・size/hash・`ZipFile.testzip()`・archive内SQLiteの`integrity_check`を検証してから正式名へ原子的に置換する。
- 必要な回帰: unreadable/消失/書込途中file、disk-full相当、CRC破損で成功archiveをpublishせず、既存同名snapshotも保持すること。

### [P1] snapshot復元がarchive全体の事前検証前に現stateを消し、展開失敗を部分復元のまま残す

- 場所: `scripts/snapshot.py:398-475`, `docs/intent/version_aware_world_and_persona.md:140-183`
- 事実: restore前に読むのは`snapshot.json`だけで、hash、CRC、member件数、必須DB、SQLite integrityを検証しない。その後`clear_for_restore()`で現stateを削除し、同じhomeへmemberを逐次`extract()`する。途中例外時は非0を返すが、抽出済みfileの除去、元stateへのrollback、auto-snapshotからの自動復旧は行わない。
- 最小再現: 現stateへ`current.txt`を置き、2member archiveの2件目extractを強制失敗させた。終了コード1、`current.txt`は消失、1件目だけ復元された部分stateが残った。
- 追加再現: data member 0件で、`snapshot.json`だけに`file_count=999`・`saiverse_version='forged'`を入れたZIPを復元した。metadata値は検証されず、終了コード0・`OK: Restored`となり、元の`current.txt`だけが削除された。
- 追加member境界: crafted archiveへ保存対象外の`snapshots/known.zip`を入れると、復元時に既存の正常な復旧点が無警告で上書きされた。save時の除外規則はrestore側で強制されていない。
- 影響: 破損ZIP、容量不足、権限/handle問題で、停止中だったworldを起動不能または世代混在へ変える。表示される「auto-snapshotを使える」は復旧可能性の案内であり、このrestore transactionの原子性ではない。
- 修正方針: archiveを別stagingへ全展開し、path containment、manifest/hash/CRC、必須file、全SQLite integrityを検証する。その後、保存対象treeを同一filesystem上でswapし、swap失敗時は元treeへ自動rollbackする。auto-snapshotは二次防御として残す。
- 必要な回帰: archive末尾破損、member欠落、SQLite破損、disk-full、rename失敗の各時点で、元home全体または完全な復元homeのどちらか一方だけが残ること。

### [P1] snapshotの起動中検出がfail-openで、検出できた場合も`--force`で破壊操作を継続できる

- 場所: `scripts/snapshot.py:77-148,280-290,404-411,531-551`, `docs/intent/version_aware_world_and_persona.md:174-183`
- 事実: 検出はmain DBの`city.API_PORT`を読み、localhostでportが開いているかだけを見る。DBなし、read/query失敗、port未設定/不正、backendが別interface/portで動作、起動直後でlisten前、port probe一時失敗はいずれも`False`（停止中）になる。さらに`save --force`と`restore --force`は`True`を検出しても継続する。Intentの安全装置は「動作中なら警告して中止」としている。
- 影響: 実際には起動中でもraw file snapshotや、稼働中DB/persona treeのclear/extractへ進める。前者は世代不整合、後者はopen connectionと復元fileのsplit-brain・部分削除を生む。
- 修正方針: process-owned lock/pid＋process identity、backend lifecycle marker、DB exclusive probeなど複数signalで判定し、「停止を証明できない」状態は拒否する。少なくともrestoreではforce bypassを廃止し、saveのlive captureが必要ならSQLite backup APIを使う別commandへ分離する。
- 必要な回帰: port未設定、listen前、DB query失敗、別host bind、stale pid、実process稼働の各状態を区別し、unknown/runningではrestoreが一切mutationしないこと。

## Findings（第5片: upgrade registry / updater再起動context）

### [P1] default upgrade handlerのimport失敗を空registryとして確定し、全entity versionを最新へ進める

- 場所: `saiverse/upgrade.py:65-86,149-215,221-280`
- 事実: `_load_default_handlers()`は`upgrade_handlers`の`ImportError`をwarningで握り潰し、`_handlers_loaded=True`にしてreturnする。startupはregistry件数0を異常とせず、各entityに対して0 handlerを実行後、`LAST_KNOWN_VERSION=target`をcommitする。同processでは再importも試みない。
- 最小再現: handler importだけを強制`ImportError`にした。registryは0件・loaded済みとなり、`LAST_KNOWN_VERSION='0.0.0'`のentity upgradeは`True`、version `1.0.0`、commit 1回だった。
- 影響: moduleのsyntax/import dependency/circular import問題が一度起きるだけで、全City/AIが必要handler未実行のまま処理済みになる。import問題を直して再起動してもversion比較でskipされ、自動回復しない。
- 修正方針: default handler import失敗はstartup fatalにし、registryをloaded扱いしない。期待handler manifestまたは最低件数・最高`to_version`をcode versionと照合し、registry構築成功前にはentityへ触れない。
- 必要な回帰: handler module不在、module内部dependency不在、syntax/import cycleでstartupが中断し、全entity versionとdataが不変であること。修正後の次回起動で再import・handler実行されること。

### [P1] handler選択が`from_version`を無視し、遷移鎖の欠落・不整合を検出せず完了versionを刻む

- 場所: `saiverse/upgrade.py:43-58,117-143,179-215`, `docs/intent/version_aware_world_and_persona.md:86-111,252`
- 事実: `UpgradeHandler.from_version`は隣接遷移元として定義され、Intentも隣接遷移を前提とする。しかし`select_handlers()`がparse・比較するのは`to_version`だけで、`from_version < handler.to_version`、直前handlerの`to_version == 次handler.from_version`、currentからtargetまでの連続性を検証しない。invalid `to_version`もwarning skipである。
- 最小再現: `from_version='9.0.0', to_version='2.0.0'`の成立不能handlerを1件登録し、current `1.0.0`→target `2.0.0`を実行した。handlerは選択・実行され、結果`True`、entity versionも`2.0.0`へ進んだ。
- 影響: handler登録漏れ、typo、cherry-pick不足、同版内のregistry不整合があっても後段handlerを誤った前提stateへ適用する。例外が出なければ欠落区間を飛び越え、以後の起動では再試行されない。
- 修正方針: startup時にscope別handler graphを検証し、currentからtargetまで適用されるedgeが連続することを保証する。data migration不要な版境界も明示no-op edgeとして登録するか、manifestで「処理不要」を宣言し、暗黙のgapと区別する。
- 必要な回帰: gap、overlap、逆向きedge、invalid version、重複edge、登録順の乱れを拒否し、正しい複数edgeだけが隣接順に一度ずつ走ること。

### [P1] self-updaterが元の起動contextを保存せず、更新成功後に別City・別DB条件で再起動し得る

- 場所: `api/routes/system.py:340-376`, `scripts/self_update.py:326-405,415-510`, `start.bat:31`, `start.sh:11,39-40`, `main.py:291-301`
- 事実:
  - update configは`city_name`、port、PID等を保存するが、実際のDB path、`--sds-url`、元CLI引数を保存しない。dependency phaseのmigration先も`~/.saiverse/user_data/database/saiverse.db`へhardcodeされる。
  - Unix再起動は`start.sh city_name`まで渡すが、同scriptは`python main.py "$CITY_NAME"`だけで元`--db-file`/`--sds-url`を復元しない。
  - Windows再起動は`start.bat`を引数なしで呼び、repositoryの`start.bat`は`python main.py city_a`固定である。設定に保存した`city_name`すら使われない。
- 最小再現: `restart_application(temp, 'city_b', 'win32')`の`Popen`引数を捕捉したところ、`['cmd', '/c', '<temp>/start.bat']`だけだった。実scriptのbackend commandは上記のとおり`city_a`固定である。
- 影響: `city_b`からUI更新するとWindowsでは`city_a`として立ち上がる。custom `--db-file`で稼働していた場合は全OSでdefault DBへ切り替わり、更新前worldが消えたように見えるか、別worldへ書き込みを始める。migration phaseも実稼働DBではないfileを更新・報告する。
- 修正方針: managerが使用中のresolved DB pathと再起動に必要な全引数・環境をupdate configへ保存し、updaterが`venv_python main.py ...`を直接同じargvで起動する。dependency migration/importにも同じresolved DBを明示的に渡し、batch/shのdefault launchへ委譲しない。
- 必要な回帰: city_b、absolute/relative custom DB、custom SDS URL、`SAIVERSE_HOME`/`SAIVERSE_USER_DATA_DIR` overrideについて、更新前後のresolved City・DB・endpointが完全一致すること。Windows/Unixで同じcontractを検証すること。

## Findings（第6片: snapshot上書き / updater停止対象 / migration CLI）

### [P1] self-updaterがport listenerのprocess identityを検証せず、SAIVerse外のprocessも強制終了する

- 場所: `scripts/self_update.py:46-179,415-466`, `api/routes/system.py:353-369`
- 事実:
  - `wait_for_port_free()`はportが5回塞がると`find_pid_for_port()`が返した全PIDを`kill_pid()`へ渡す。configの`main_pid`、executable path、command line、project root、親子関係、process start timeとの照合はない。
  - 30秒後にも同じbackend portの全listenerを再度killする。frontend portは待機せず、固定3000番をLISTENする全PIDを即killする。
  - config PID自体もprocess再利用を識別するstart tokenを持たず、同じ数値の別processかどうかを検証しない。
- 最小再現: port checkを5回`False`、PID列挙を`[4242]`、6回目を`True`にした。identity情報を一切与えていないのにPID 4242がkill対象となり、関数は最終的に`True`を返した。
- 影響: shutdown後に別applicationが同じportを取得した場合、もともとport 3000を使っていた別frontendがある場合、PIDが再利用された場合に、SAIVerse更新が無関係なprocessを強制終了する。Windowsでは`taskkill /F`、Unixでは最終的に`SIGKILL`まで送る。
- 修正方針: API側でspawn済みchild PIDとprocess identity（start time・executable・project marker）をconfigへ記録し、その集合だけを停止する。portは停止完了の観測に限定し、未知listenerが残る場合はupdateを中断してPID情報を報告する。固定frontend portではなく実際のmanaged childを追跡する。
- 必要な回帰: main/child正常終了、PID再利用、port横取り、同じportの無関係process、権限不足、kill失敗で、所有確認済みprocess以外へsignalを送らず、未知listenerがある場合はcode updateへ進まないこと。

### [P2] snapshot同名上書きが旧archiveを先に削除し、publish失敗時に既知の復旧点まで失う

- 場所: `scripts/snapshot.py:270-326`
- 事実: `save --force`は新snapshotをtmpへ作った後、既存`snap_path.unlink()`、`tmp_path.rename(snap_path)`の順に公開する。rename失敗時はcatch節がtmpを削除するが、先にunlinkした旧snapshotを復元しない。コメントの「アトミックに置き換え」と実装が一致しない。
- 最小再現: 正常な既存`same.zip`を置き、新tmp作成後の`Path.rename()`だけを強制失敗させた。終了コード1となり、旧`same.zip`は残らなかった。
- 影響: antivirus/file watcher、権限、filesystem異常などpublish境界の失敗一回で、新snapshotだけでなく以前検証済みの同名復旧点も失う。不可逆migration直前に`--force`で同じ名前を再利用する運用ほど危険になる。
- 修正方針: 同一filesystem上で`os.replace(tmp, destination)`を使い、旧fileを先にunlinkしない。Windows上の置換制約がある場合は旧fileを退避名へrenameし、新file公開失敗時に自動で戻す二段swapにする。
- 必要な回帰: destination既存、rename/replace失敗、file watcher保持、disk-full相当で、旧または新の完全なarchiveが必ず一つ残ること。

### [P2] migration CLIが明示された不存在DBを「変更なし」として終了0にする

- 場所: `database/migrate.py:20-42,203-232,891-912`
- 事実: `needs_migration()`はpath不存在で`False`を返し、CLIは「スキーマに変更はありません」と正常終了する。`--force`でも`migrate_database_in_place()`が「不要」とreturnするため終了0である。明示`--db`のtypoと、本当にmigration不要な既存DBを区別しない。
- 最小再現: 存在しない`.tmp_definitely_missing_saiverse.db`を`--db`へ渡した。表示は「マイグレーションは不要」、process exit 0、DB fileも作成されなかった。
- 影響: shell/updater/運用automationは対象DBが検査・更新されたと誤認して後続phaseへ進む。path解決ミスが成功に見え、実world DBだけが旧schemaのまま残る。
- 修正方針: explicit `--db`が存在しなければ非0終了する。新規DB作成を許すなら別`--create` contractに分離し、既存DB migrationと混同しない。CLI mainは結果型を受けて`no_change / migrated / failed / target_missing`を終了コードと構造化ログへ反映する。
- 必要な回帰: explicit missing path、default missing path、既存差分なし、additive成功、full rewrite成功/失敗、rollback失敗の各終了コードを固定すること。

### migration CLIの他境界で確認済み（新規findingなし）

- schema inspectionとadditive適用の未捕捉例外、backup file move失敗はCLI最上位まで伝播して非0になる。full rewrite本体・rollback失敗の握り潰しは第1片の既出P1であり、今回重複計上していない。

## Findings（第7片: updater再起動確認 / backup世代衝突 / snapshot metadata追加検証）

### [P1] self-updaterが再起動processの成立を確認せず、service停止のままupdate完了・config削除にする

- 場所: `scripts/self_update.py:376-405,415-521`
- 事実:
  - `restart_application()`はstart script不在時にerror logを出すだけで成否を返さない。scriptがある場合も`Popen()`した時点で完了し、processの即時exit、backend port listen、health endpoint、期待version/Cityを確認しない。
  - `main()`は戻り値を見ず、直後に`.update_config.json`を削除して`Self-update complete!`を記録する。update前backend/frontendは既に停止済みである。
- 最小再現: start scriptを置かない一時projectで、code update成功・dependency phase完了を固定して`main()`を実行した。`start.bat not found`が記録された後も例外・失敗returnはなく、update configは削除された。
- 影響: start script欠落、frontend build失敗、backend startup migration失敗、port競合、import errorが起きても、更新processは完了扱いで終了しSAIVerseは停止したままになる。再実行に必要なconfigも消え、UIからの復旧操作もできない。
- 修正方針: restartを結果型にし、child PID捕捉後に一定時間の生存、期待backend health、version、City、resolved DBを確認して初めて成功とする。失敗時はconfigと診断logを保持し、可能なら旧code/依存関係でのrollback restartを行う。update全体の終了コードにも反映する。
- 必要な回帰: script不在、spawn例外、即時exit、health timeout、wrong City/version、正常起動について、完了log・config cleanup・rollbackの分岐が一致すること。

### [P2] main DB backup名がミリ秒精度で衝突し、別世代を同一fileへ無警告で上書きする

- 場所: `database/backup.py:31-84`
- 事実: backup名はmicrosecondsを生成後`[:19]`で切り、ミリ秒精度`YYYYmmdd_HHMMSS_mmm`にする。既存pathの排他作成や連番/UUIDはなく、`sqlite3.connect(backup_path)`は同名fileをそのままdestinationとして開く。
- 最小再現: `datetime.now()`を同一ミリ秒に固定し、1行時点と2行時点で連続backupした。二回のreturn Pathは同一、directory内backupは1fileだけで、生存fileの内容は2行（後の世代）へ上書きされていた。
- 影響: 同じmain DBを使う複数Cityの同時startupや短時間の手動backupで、作成回数と復旧世代数が一致しない。並行実行では同一destinationへのSQLite backup競合に加え、一方の失敗cleanupが共有pathを削除する可能性もある。
- 修正方針: exclusive create可能なUUID/高精度timestamp＋process-safe連番で一意名を確保し、tmp固有fileへbackup・検証後にpublishする。manifestへsource DB identity、created_at、source versionを持たせ、retentionは検証済み世代だけを対象にする。
- 必要な回帰: 同一時刻固定、複数thread/process、同一DBを使う複数City、片方失敗で、各成功callが別の完全fileを返し、失敗callが他世代を削除しないこと。

### snapshot metadata真正性の追加確認（新規findingなし）

- 第4片P1に追記したとおり、metadata-only ZIPでも`file_count`・version・member実数を検証せず成功復元となる。これは既出の「archive全体の事前検証前に現stateを消す」と同じ根因なので重複計上していない。

## Findings（第8片: backup retention / snapshot member / updater rollback・一次監査完了）

### [P2] backup retentionが復元可能性を検証せずmtimeだけで選別し、正常世代を削除して破損fileを残す

- 場所: `database/backup.py:86-113,135-160`
- 事実: `_prune_old_backups()`と`get_recent_backups()`はpattern一致fileをSQLiteとして開かず、`st_mtime`降順だけで「recent」と判定する。filename timestamp、size、`integrity_check`、source identity、作成完了markerを見ない。
- 最小再現: pattern一致の正常SQLite backupと、内容`not sqlite`・mtimeを翌日にした破損backupを置き、`keep_count=1`でpruneした。正常backupは削除され、破損fileだけが残り、`PRAGMA integrity_check`は`DatabaseError`になった。
- 影響: clock skew、file copy/restore、外部toolによるtouch、途中生成物の改名でmtimeが新しくなると、retentionが最後の正常復旧点を能動的に削除する。list APIも破損fileを最新backupとして提示する。
- 修正方針: backup完成時にmanifest/complete markerを原子的にpublishし、prune前にSQLite header・`integrity_check`・source DB identityを検証する。破損/unknown fileは正常世代のkeep枠へ数えずquarantineし、選別はimmutableなmanifest created_at＋一意世代IDで行う。
- 必要な回帰: zero-byte、非SQLite、integrity failure、未来/同一mtime、copyでmtime更新、正常世代混在時に、指定数の正常backupを必ず保持し破損fileがそれを押し出さないこと。

### snapshot member境界で確認済み（既出findingへの追加・新規findingなし）

- crafted archiveはsave時の除外対象である`backups/`・`snapshots/`・`user_data/logs/`にも展開できるため、既存復旧点を上書きできる。第4片P1の「manifest/member事前検証なし」に追記し重複計上していない。
- `../escaped.txt` memberは現在のPython `zipfile.extract()`によりhome直下の`escaped.txt`へ正規化され、home外には作成されなかった。標準的なdot-dot traversalについては追加findingなし。ただし既存symlink/junction経由は別のfilesystem境界なので、修正時のpath containment回帰へ含める。

### updater rollback境界で確認済み（既出finding・新規findingなし）

- git/ZIP code、dependency、DB migration、playbook import、frontend installを一つのrollback unitへ戻す機構はない。これは第1片のphase失敗後の混在restartと第2片のZIP部分上書きで既に計上済みである。
- git stash pop conflict時はworking treeを`reset --hard HEAD`するが、conflictしたstash entry自体はGit仕様上保持される実装意図で、少なくとも当該経路は「local変更の唯一copyも削除する」とは確認されなかった。回復が手動である点は既出updater運用問題の範囲に留める。

## 修正追跡（2026-07-16・第一陣）

以下を修正・回帰固定した（合計 **P1×4 / P2×4**）。finding総数は履歴として減算しない。

- [P1] full rewrite失敗をrollback後も必ず例外化し、rollback失敗時はoriginal/backup/partial pathと両例外をfatal errorへ残す。
- [P1] 削除旧列を写すpost-hookをstrict化し、壊れた`tasks_json`、state変換、INSERT等の失敗でfull rewrite全体をrollbackする。
- [P1] DB entity versionが実行codeより新しい場合は変更せずstartup upgradeを失敗扱いにする。
- [P1] 必須desire正規化backfillの例外をstartupへ伝播する。
- [P2] SQLite backupのsource/destination connectionを`closing()`で明示closeする。
- [P2] backup名をfull microseconds＋UUID＋衝突guardにし、同一時刻・複数processで別世代を上書きしない。
- [P2] snapshot同名更新を旧fileの先行unlinkから`os.replace()`へ変更し、publish失敗時に旧archiveを保持する。
- [P2] 明示`--db`の不存在をCLI error（非0）にし、migration関数自体も`FileNotFoundError`にする。

回帰: `tests/test_audit_batch_one_safety.py`でWindows handle解放、同一時刻backup、full rewrite rollback＋raise、malformed旧tasks、desire失敗伝播、future version、snapshot replace失敗、CLI不存在を固定した。

## 修正追跡（2026-07-16・第二陣）

- mutation前に同期・integrity検証済み `pre_upgrade` backupを作り、通常startup世代とは独立retentionにした。manifest不在・size不一致・`integrity_check`失敗世代はkeep/list対象にしない。
- 停止状態専用のmain DB restore entrypointを `python -m database.backup --db <path> restore <backup>` として追加し、source DB identity、pre-restore世代、staging、main DB＋WAL/SHM rollbackを実装した。
- world snapshotをformat v2へ上げ、SHA-256/size manifest、CRC、member境界、必須main DB、全SQLite integrityをmutation前に検証する。restoreはstaging treeをswapし、失敗時に旧worldを戻す。
- process-owned markerは同じhome内の複数Cityを許容しつつ、一つでも稼働中またはidentity不明ならsnapshot/restoreを拒否する。`--force`での稼働中bypassは廃止した。
- World Editorのbackup/restore/deleteはcanonical snapshot engineへの薄い入口にし、旧raw copy/delete実装を無効化した。`backups/`はsnapshot/swap対象外なのでpersona `memory.db`個別backupは維持される。
- updaterはbat/sh/PowerShell/UIを `scripts/update_engine.py` へ集約した。clean Git fast-forward、更新前world snapshot、phase fail-stop、同一argv/City/DB identity再起動、health確認、code rollbackを共通contractにし、port listenerの一括killを撤去した。
- unsafeなZIP overlayはfail-closedで廃止した。非Git配布を戻すには署名済みrelease manifestによるstaging/mirror swapが必要。
- upgrade registry import、handler chain gap/overlap/逆向きedge、malformed data、未来versionをfail-closedにした。external `memory.db`通知はdeterministic upgrade idで冪等化し、City移動profileは完全一致SAIVerse versionを要求する。

回帰: `tests/test_audit_second_batch_world.py`、`tests/test_update_engine_safety.py`、既存upgrade handler suiteで固定した。
