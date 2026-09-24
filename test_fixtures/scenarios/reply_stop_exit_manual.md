# 「返事が途中で止まった回の出口」をブラウザで確かめる手順

設計: `docs/intent/reply_stop_exit.md`。この手順は隔離テスト環境 (`test_data/`) だけを使い、本番の世界 (`~/.saiverse/`、ポート 8000 / 3000) には触れない。ペルソナの返事は台本つきの偽 LLM が作るので、実際の LLM は一度も呼ばれず、課金も起きない。

## 登場するもの

| もの | 場所 | 役 |
|---|---|---|
| 偽 LLM サーバー | `test_fixtures/scenarios/scripted_llm_server.py` (ポート 18097) | 台本どおりに返事をする。台本はサーバーを立て直さずに切り替えられる |
| テストバックエンド | `test_fixtures/scenarios/start_scripted_backend.bat` (ポート 18000) | 隔離環境の SAIVerse。実 LLM の鍵をすべて無効な値で上書きして起動する |
| テストフロントエンド | `test_fixtures/start_test_frontend.bat` (ポート 18010) | 18000 のバックエンドにつながる画面 |
| 合成ペルソナ | `Stub Persona` (`test_persona_stub`) | すべてのモデルが偽 LLM を向いている。部屋「Stub Room」に一人でいる |
| ヘッドレス確認 | `test_fixtures/scenarios/check_reply_stop_exit.py` | ブラウザを使わずに同じ場面を API で踏む |

## 1. 環境を用意する (初回と、やり直したいとき)

```bat
.venv\Scripts\python.exe test_fixtures\setup_test_env.py
```

テスト DB を作り直し、`Stub Room` と `Stub Persona`、偽 LLM を指すモデル設定 (`test_data/user_data/models/scripted-llm.json` と `providers/scripted-llm.json`) を置く。テスト DB の中身 (会話の記録) は消える。ペルソナの記憶 (`test_data/.saiverse/personas/`) は消えない — 消したいときは `--reset-memory` も付ける。

## 2. 三つのプロセスを立てる (端末を三つ)

端末 1 — 偽 LLM サーバー:

```bat
.venv\Scripts\python.exe test_fixtures\scenarios\scripted_llm_server.py serve
```

端末 2 — テストバックエンド (偽 LLM の後に):

```bat
test_fixtures\scenarios\start_scripted_backend.bat
```

`Uvicorn running on http://127.0.0.1:18000` が出れば起動済み。**実装を書き換えたら、このバックエンドを止めて立て直す** (読み込んだ時点のコードで動くため)。

端末 3 — テストフロントエンド:

```bat
test_fixtures\start_test_frontend.bat
```

ブラウザで `http://localhost:18010` を開き、サイドバーの部屋一覧から **Stub Room** へ移動する。部屋にいるのは Stub Persona だけ (ほかのテストペルソナは別の部屋にいて、話しかけなければ動かない)。

台本の切り替えは、もう一つ端末を開いて次の形で打つ:

```bat
.venv\Scripts\python.exe test_fixtures\scenarios\scripted_llm_server.py preset <名前>
.venv\Scripts\python.exe test_fixtures\scenarios\scripted_llm_server.py status
.venv\Scripts\python.exe test_fixtures\scenarios\scripted_llm_server.py requests --tail 10
```

## 3. 場面を踏む順番

画面の見え方として書いたものは、`frontend/src/app/page.tsx` と `messages.json` を読んで組んだ**期待**で、ブラウザではまだ誰も確かめていない。サーバー側 (イベントの中身・印・通告) はヘッドレス確認で確かめ済み (末尾の節)。

### 場面 A — スペルのあとで止まる → エラー札と「続きの生成」

1. 台本を切り替える: `preset spell_then_429`
2. Stub Room で何か一言送る (内容は何でもよい。例「手帳見てくれる？」)
3. 画面で見るところ:
   - ペルソナの吹き出しが一つ出る。本文は「ちょっと待ってね、手帳を開いて確かめてみる。」で、その下にスペル「手帳を開く」の結果の折りたたみが付いている
   - その後、数秒 (偽 LLM が 429 を返し、クライアントが 9 回まで再試行するため 7〜8 秒ほど) でエラー札が出る。札の文面は「APIの利用制限に達しました。…」の下に「API利用制限に達しています。しばらく時間を置いてから、ペルソナの発言に表示されている「続きの生成」ボタンを押してください。」の案内
   - **ペルソナの吹き出しに「続きの生成」ボタンが出る** (エラー札ではなく、ペルソナの発言の側)。ユーザー発言の側に「再送」は出ない
   - 吹き出しの後ろに、中断の通告「(スペルの結果を受け取った後、続きの発言の前に中断されました)」の行が並ぶ (表示のされ方は画面の host 行の扱いに従う)
4. ページを再読み込みして、同じ吹き出しに「続きの生成」が出たままであることを確かめる (履歴 API の `interrupted` からの復元)

### 場面 B — 「続きの生成」を押して二言目

1. **押す前に**台本を切り替える: `preset reply_plain` (場面 A の台本は 429 を返し続けるので、切り替えないと続きも 429 になる)
2. 場面 A の吹き出しの「続きの生成」を押す
3. 画面で見るところ:
   - ペルソナの新しい吹き出し「お待たせ。手帳を見てきたよ — まだ何も書いていなかった。これが続きの発言です。」が出る
   - 元の吹き出しの「続きの生成」ボタンが消える
   - 新しい通告は増えない
4. 再読み込みしても、元の吹き出しにボタンが戻らないことを確かめる

### 場面 C — 発言の途中で接続が切れる (スペルなし)

1. `preset cut_abort`
2. 一言送る
3. 画面で見るところ: 長い発言が「…その話をしようと思っていたんだ。最初に」で途切れ、エラー札 (現状の文面は「LLMでエラーが発生しました」+ 既定の案内「ここまでの発言は記録に残っています。少し待ってから、…「続きの生成」ボタンを押してください。…」) が出て、途切れた吹き出しに「続きの生成」が出る。通告は「(ここで発言が中断されました)」
4. `preset reply_plain` にしてから「続きの生成」を押すと二言目が出る

### 場面 D — スペルのあとの続きの途中で接続が切れる (既知の欠陥の確認)

1. `preset spell_then_cut_abort`
2. 一言送る
3. ヘッドレス確認では、途切れた二つ目の吹き出しに印と通告は付くが、**画面へのエラー札も案内も届かない** (下の「ヘッドレス確認で見つかったこと」1)。画面で「続きの生成」がその場で出るか、再読み込みで初めて出るかを見る

### 参考 — 黙って途切れる回

`preset cut_eof` / `preset spell_then_cut_eof` は、偽 LLM が終わりの印を送らずにストリームを正常に閉じる形。現状は言い切った発言として扱われ、印も通告も付かない (openai 互換の経路はこれを見分けられない — 下の 2)。

## 4. 終わったら

- 偽 LLM とバックエンドとフロントエンドの端末で Ctrl+C
- 台本を空に戻したいだけなら `preset empty` (既定の一文で答え続ける)

## ヘッドレス確認 (ブラウザを使わない)

三つのうち偽 LLM とバックエンドだけ立てた状態で:

```bat
.venv\Scripts\python.exe test_fixtures\scenarios\check_reply_stop_exit.py              rem 場面 A と B
.venv\Scripts\python.exe test_fixtures\scenarios\check_reply_stop_exit.py --scenario all
```

ユーザーを Stub Room へ移し、台本を切り替えながら `/api/chat/send` と `/api/chat/continue` の NDJSON を読み、テスト DB の `building_messages` を読み取り専用で開いて、印 (`metadata._interrupted`) と通告 (host 行) を確かめる。本番のポートと `test_data/` の外の DB は拒否する。

### ヘッドレス確認で見つかったこと (2026-09-25)

1. **スペルのあとの続きのストリームが途中で切れた回 (場面 D)、印と通告は付くのにエラー札が出ない。** openai 互換のクライアントはストリームの途中の例外を LLMError に包まない (`llm_clients/openai.py` の `_stream_text_mode` は包まず、ツールあり経路だけが包む)。スペルの周回の包括 except (`sea/runtime_llm.py` の「spell loop fatal error」) は、続きの呼び出しの失敗を LLMError のときだけ「続きの失敗」として投げ直すので、包まれていない例外は「スペル系の内部エラー」に降格され、返事は例外なしで閉じる。後始末は `cause=none` で走って印と通告を置くが、画面への知らせ (エラー札・情報の知らせ) はどれも出ない。スペルなしで同じ切れ方をした場面 C はエラー札 + 案内が出るので、同じ失敗が周回の前後で違う画面になる。
2. **openai 互換の経路では、ストリームが黙って途切れた回 (終わりの印なし) を見分けられない。** 「サーバーが切った」申告 (`consume_stream_error`) は Gemini クライアントにしか無い。本件の設計の外にある既存の形。
