"""NIM の構造化出力が、会話と同じモデル設定で送られること。

NIM の構造化出力は SDK を通らない生 HTTP 経路で送られる
(``llm_clients/nvidia_nim.py: _create_nim_structured_output_via_tool``)。この経路が
モデル定義の ``request_kwargs`` から一部のキーだけを手で写していた間は ``extra_body`` が
落ち、同じモデルが会話では thinking あり、構造化出力では thinking なしで動いていた。
エラーにはならないので、出力の質にしか現れない。

既存の NIM テストはクライアントを直接組み立てていて ``request_kwargs`` を持たせて
いなかったので、この欠落は構造上映らなかった。ここでは同梱のモデル定義を factory 経由で
解決してクライアントを作り、実際に送られる HTTP リクエストを MockTransport で読む。
実 API は呼ばない。
"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SAIVERSE_SKIP_TOOL_IMPORTS", "1")

import httpx  # the NIM raw path posts with httpx
import httpx2  # openai 3.x runs on httpx2

from llm_clients.factory import get_llm_client
from saiverse import data_paths, model_configs, provider_configs

_FLASH_0731 = "nim-deepseek-v4-flash-0731"
_API_KEY = "test-nim-key"
_FORCED_TOOL_CHOICE = {"type": "function", "function": {"name": "_structured_output"}}
_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
_MESSAGES = [{"role": "user", "content": "probe"}]
_PROBE_TOOL = {
    "type": "function",
    "function": {"name": "probe", "parameters": {"type": "object", "properties": {}}},
}
_RAW_RESPONSE = {"choices": [{"message": {"tool_calls": [
    {"function": {"name": "_structured_output", "arguments": '{"ok": true}'}},
]}}]}
_SDK_RESPONSE = {
    "id": "x", "object": "chat.completion", "created": 0, "model": "m",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "hi"},
        "finish_reason": "stop",
    }],
}


def _read_builtin(subdir: str, name: str) -> dict:
    """builtin_data の定義をローダーを通さずに読む。

    ローダーを通すと、テストを走らせる機械に同じ名前の user_data 上書きがあった場合に、
    SAIVerse が同梱しているものではなくそちらを検べてしまう。
    """
    path = data_paths.BUILTIN_DATA_DIR / subdir / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _tool_names(body: dict) -> list:
    return [tool["function"]["name"] for tool in body["tools"]]


class TestNimStructuredOutputRequest(unittest.TestCase):

    def setUp(self):
        self.provider = _read_builtin(data_paths.PROVIDERS_DIR, "nvidia_nim")
        # provider_configs.load_configs() が builtin_data の定義に押す印
        self.provider["source"] = provider_configs.SOURCE_BUILTIN
        env = patch.dict(os.environ, {self.provider["api_key_env"]: _API_KEY})
        env.start()
        self.addCleanup(env.stop)

    def _client(self, key, model_json, *, sdk_handler=None):
        """model_configs と同じ解決をかけた定義を、factory でクライアントにする。

        プロバイダの参照は組み立ての間ずっと同梱の NIM 定義に固定する。factory は
        資格情報の検査でプロバイダを読み直すので、途中で外すと user_data 側を見る。
        sdk_handler を渡すと、SDK 経路の送信先を MockTransport にする。
        """
        with patch("saiverse.provider_configs.get_provider", return_value=self.provider):
            resolved = model_configs._resolve_provider_ref(dict(model_json))
            resolved["source"] = data_paths.LAYER_BUILTIN
            with patch.object(model_configs, "MODEL_CONFIGS", {key: resolved}):
                if sdk_handler is None:
                    return get_llm_client(key, resolved["provider"], 8192)

                from openai import OpenAI as RealOpenAI

                def sdk_with_mock_transport(**kwargs):
                    return RealOpenAI(
                        **kwargs,
                        http_client=httpx2.Client(transport=httpx2.MockTransport(sdk_handler)),
                    )

                with patch("llm_clients.openai.OpenAI", side_effect=sdk_with_mock_transport):
                    return get_llm_client(key, resolved["provider"], 8192)

    def _send_structured(self, client, **generate_kwargs):
        """構造化出力を一回走らせ、生 HTTP 経路が送ったリクエストを返す。"""
        sent = []
        real_client = httpx.Client

        def handler(request):
            sent.append(request)
            return httpx.Response(200, json=_RAW_RESPONSE)

        def client_with_mock_transport(*args, **kwargs):
            return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

        with patch("httpx.Client", side_effect=client_with_mock_transport):
            result = client.generate(
                list(_MESSAGES), tools=[], response_schema=_SCHEMA, **generate_kwargs,
            )
        self.assertEqual(result, '{"ok": true}')
        self.assertEqual(len(sent), 1)
        return sent[0]

    def test_flash_0731_structured_output_runs_with_thinking(self):
        model_json = _read_builtin(data_paths.MODELS_DIR, _FLASH_0731)

        request = self._send_structured(self._client(_FLASH_0731, model_json))
        body = json.loads(request.content)

        self.assertEqual(body["chat_template_kwargs"], {"thinking": True})
        self.assertNotIn("extra_body", body)
        # Structured output is still the forced call to the dummy tool.
        self.assertEqual(_tool_names(body), ["_structured_output"])
        self.assertEqual(body["tool_choice"], _FORCED_TOOL_CHOICE)
        self.assertEqual(body["model"], model_json["model"])
        self.assertEqual(body["n"], 1)
        # The parameter defaults factory applies from the definition still ride along.
        for name, spec in model_json["parameters"].items():
            self.assertEqual(body[name], spec["default"], name)
        self.assertEqual(str(request.url), f"{self.provider['base_url']}/chat/completions")
        self.assertEqual(request.headers["authorization"], f"Bearer {_API_KEY}")

    def test_muse_glimmer_structured_output_runs_with_low_reasoning_effort(self):
        """Muse Glimmer 30B は既定の考える深さだと短い返事にも 20 秒前後かかるため、定義の
        パラメータ既定で low にしている (チュートリアルの NIM 設定の軽量・Memory Weave)。
        構造化出力の生 HTTP 経路でも、その既定が送られる。"""
        key = "nim-muse-glimmer-30b"
        model_json = _read_builtin(data_paths.MODELS_DIR, key)

        body = json.loads(self._send_structured(self._client(key, model_json)).content)

        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["tool_choice"], _FORCED_TOOL_CHOICE)

    def test_every_shipped_nim_extra_body_reaches_structured_output(self):
        """同梱の NIM モデルが extra_body に書いた指定は、構造化出力でも全部送られる。"""
        checked = []
        for path in sorted((data_paths.BUILTIN_DATA_DIR / data_paths.MODELS_DIR).glob("*.json")):
            model_json = json.loads(path.read_text(encoding="utf-8"))
            extra_body = (model_json.get("request_kwargs") or {}).get("extra_body")
            if model_json.get("provider_ref") != "nvidia_nim" or not isinstance(extra_body, dict):
                continue
            checked.append(path.stem)
            with self.subTest(model=path.stem):
                body = json.loads(self._send_structured(self._client(path.stem, model_json)).content)
                for field, value in extra_body.items():
                    self.assertEqual(body.get(field), value, field)
        # 対象が一枚も見つからずに素通りで緑になるのを防ぐ。
        self.assertIn(_FLASH_0731, checked)

    def test_structured_output_keys_cannot_be_replaced_by_config(self):
        """request_kwargs や extra_body に tools / tool_choice があっても構造化出力の値が勝つ。"""
        model_json = {
            "model": "vendor/owned-keys-probe",
            "provider_ref": "nvidia_nim",
            "request_kwargs": {
                "tool_choice": "none",
                "n": 3,
                "extra_body": {
                    "tools": [{"type": "function", "function": {"name": "other", "parameters": {}}}],
                    "tool_choice": "auto",
                    "model": "vendor/other",
                    "messages": [],
                    "n": 2,
                    "chat_template_kwargs": {"thinking": True},
                },
            },
        }
        client = self._client("nim-owned-keys-probe", model_json)

        with self.assertLogs(level="WARNING") as logs:
            body = json.loads(self._send_structured(client).content)

        self.assertEqual(_tool_names(body), ["_structured_output"])
        self.assertEqual(body["tool_choice"], _FORCED_TOOL_CHOICE)
        self.assertEqual(body["model"], "vendor/owned-keys-probe")
        self.assertEqual(body["n"], 1)
        self.assertIn("probe", json.dumps(body["messages"]))
        # The rest of extra_body still arrives.
        self.assertEqual(body["chat_template_kwargs"], {"thinking": True})
        # Ignoring configuration without a word would be another silent difference.
        self.assertTrue(any("tool_choice" in line for line in logs.output), logs.output)

    def test_raw_request_matches_sdk_request_for_the_same_config(self):
        """同じモデル設定なら、生 HTTP 経路は SDK 経路と同じボディ・クエリ・ヘッダーで送る。

        SDK がどのキーをボディの外へ回し、extra_body をどちらの優先で合流させるかは、
        ソースを読んだ理解ではなく実物の SDK に送らせて突き合わせる。SDK 側は同じ
        クライアントのツール付き呼び出し (_generate_tool_detection) で、構造化出力と
        同じく tools / tool_choice / n=1 を持つ。
        """
        model_json = {
            "model": "vendor/parity-probe",
            "provider_ref": "nvidia_nim",
            "request_kwargs": {
                "temperature": 0.3,
                "top_p": 0.9,
                "seed": 7,
                "timeout": 30,
                "extra_query": {"probe": "1"},
                "extra_headers": {"X-Probe": "a"},
                "extra_body": {"chat_template_kwargs": {"thinking": True}, "top_p": 0.5},
            },
        }
        sdk_sent = []

        def sdk_handler(request):
            sdk_sent.append(request)
            return httpx2.Response(200, json=_SDK_RESPONSE)

        client = self._client("nim-parity-probe", model_json, sdk_handler=sdk_handler)
        client.generate(list(_MESSAGES), tools=[_PROBE_TOOL])
        raw = self._send_structured(client)
        self.assertEqual(len(sdk_sent), 1)
        sdk = sdk_sent[0]

        def comparable(request):
            body = json.loads(request.content)
            return {k: v for k, v in body.items() if k not in ("messages", "tools", "tool_choice")}

        self.assertEqual(comparable(raw), comparable(sdk))
        self.assertEqual(dict(raw.url.params), dict(sdk.url.params))
        self.assertEqual(raw.headers.get("x-probe"), sdk.headers.get("x-probe"))
        # 両側が同時に変わっても気づけるよう、突き合わせで確かめた中身も固定する。
        self.assertEqual(comparable(raw)["top_p"], 0.5)  # extra_body wins over the named parameter
        self.assertEqual(comparable(raw)["chat_template_kwargs"], {"thinking": True})
        self.assertNotIn("timeout", comparable(raw))
        self.assertEqual(dict(raw.url.params), {"probe": "1"})
        self.assertEqual(raw.headers.get("x-probe"), "a")
        # request_kwargs.timeout is the wait on both paths.
        self.assertEqual(raw.extensions["timeout"], sdk.extensions["timeout"])
        self.assertEqual(raw.extensions["timeout"]["read"], 30.0)

    def test_structured_output_without_timeout_keeps_waiting_120_seconds(self):
        """request_kwargs に timeout が無い設定 (null を含む) では、この経路は今までどおり 120 秒待つ。"""
        for name, request_kwargs in (
            ("no-request-kwargs", None),
            ("no-timeout", {"top_p": 0.9}),
            ("null-timeout", {"timeout": None}),
        ):
            with self.subTest(name):
                model_json = {"model": "vendor/timeout-probe", "provider_ref": "nvidia_nim"}
                if request_kwargs is not None:
                    model_json["request_kwargs"] = request_kwargs
                request = self._send_structured(self._client("nim-timeout-probe", model_json))
                self.assertEqual(request.extensions["timeout"]["read"], 120.0)

    def test_timeout_that_is_not_a_positive_number_fails_before_sending(self):
        """数値でない (または 0 以下の) timeout では、この経路は送らずに失敗する。

        extra_body の形の検査と同じ扱い。既定の 120 秒に黙って置き換えると、呼び方に
        よって別の待ち時間で動く形に戻る。
        """
        for value, expected in (
            ("30", TypeError),
            (True, TypeError),
            ({"read": 30}, TypeError),
            (0, ValueError),
            (-1, ValueError),
        ):
            with self.subTest(timeout=value):
                client = self._client("nim-timeout-probe", {
                    "model": "vendor/timeout-probe",
                    "provider_ref": "nvidia_nim",
                    "request_kwargs": {"timeout": value},
                })
                with patch("httpx.Client") as http_client:
                    with self.assertRaises(RuntimeError) as raised:
                        client.generate(list(_MESSAGES), tools=[], response_schema=_SCHEMA)
                http_client.assert_not_called()
                self.assertIsInstance(raised.exception.__context__, expected)

    def test_temperature_argument_still_outranks_request_kwargs(self):
        model_json = {
            "model": "vendor/temperature-probe",
            "provider_ref": "nvidia_nim",
            "request_kwargs": {"temperature": 0.7},
        }
        client = self._client("nim-temperature-probe", model_json)

        self.assertEqual(json.loads(self._send_structured(client).content)["temperature"], 0.7)
        self.assertEqual(
            json.loads(self._send_structured(client, temperature=0.2).content)["temperature"], 0.2,
        )

    def test_extra_body_of_wrong_shape_fails_on_both_paths(self):
        """extra_body がオブジェクトでない設定は、SDK 経路では送信前に失敗する。

        生 HTTP 経路だけがそれを黙って落として送ると、呼び方によって別の設定で動く形に
        戻る。だからこちらも送らずに失敗させる。
        """
        model_json = {
            "model": "vendor/shape-probe",
            "provider_ref": "nvidia_nim",
            "request_kwargs": {"extra_body": "chat_template_kwargs"},
        }
        sdk_sent = []

        def sdk_handler(request):
            sdk_sent.append(request)
            return httpx2.Response(200, json=_SDK_RESPONSE)

        client = self._client("nim-shape-probe", model_json, sdk_handler=sdk_handler)

        with self.assertRaises(Exception):
            client.generate(list(_MESSAGES), tools=[_PROBE_TOOL])
        self.assertEqual(sdk_sent, [])

        with patch("httpx.Client") as http_client:
            with self.assertRaises(RuntimeError):
                client.generate(list(_MESSAGES), tools=[], response_schema=_SCHEMA)
        http_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
