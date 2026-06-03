"""Regression tests for playbook create/update validation.

報告不具合: モデル管理 UI で既存 playbook の router_callable を切り替えて「更新」
すると `Schema validation error: ... nodes Input should be a valid list` で落ちる。

根本原因: nodes_json カラムは playbook 全体 (name/nodes/start_node/...) を格納する
仕様 (runtime ローダ SEARuntime._load_playbook_from_db が
`PlaybookSchema(**json.loads(nodes_json))` で読む) なのに、create/update が呼ぶ
`_validate_playbook_data` だけが nodes_json を「nodes 配列」と誤認していた。

ここでは全体構造の nodes_json が検証を通ること、bare な node list が弾かれること、
router_callable 切替が通ることを固定する。
"""
import json
import unittest

from fastapi import HTTPException

from api.routes.world import _validate_playbook_data


# 自己完結した最小 playbook (外部 subplay 参照なし → graph validation が安定)
BASE_PLAYBOOK = {
    "name": "test_validate_pb",
    "description": "fixture playbook",
    "input_schema": [{"name": "input", "description": "User input"}],
    "router_callable": False,
    "user_selectable": False,
    "nodes": [
        {"id": "gen", "type": "llm", "action": "decide something", "next": "done"},
        {"id": "done", "type": "llm", "action": "finish up", "speak": True, "next": None},
    ],
    "start_node": "gen",
}


def _schema_json(data: dict) -> str:
    """import_playbook が組み立てるのと同じ表示用メタ (検証には使われない)。"""
    return json.dumps(
        {
            "name": data["name"],
            "description": data.get("description", ""),
            "input_schema": data.get("input_schema", []),
            "start_node": data.get("start_node"),
        },
        ensure_ascii=False,
    )


class TestValidatePlaybookData(unittest.TestCase):
    def test_full_playbook_nodes_json_passes(self):
        """GET が返す nodes_json = playbook 全体。これがそのまま検証を通る。"""
        data = dict(BASE_PLAYBOOK)
        nodes_json = json.dumps(data, ensure_ascii=False)
        # 例外が出なければ成功
        _validate_playbook_data(
            data["name"], data["description"], nodes_json, _schema_json(data)
        )

    def test_router_callable_toggle_passes(self):
        """報告された操作そのもの: router_callable を false→true にして更新。"""
        data = dict(BASE_PLAYBOOK)
        data["router_callable"] = True
        nodes_json = json.dumps(data, ensure_ascii=False)
        _validate_playbook_data(
            data["name"], data["description"], nodes_json, _schema_json(data)
        )

    def test_bare_node_list_rejected(self):
        """旧バグの「nodes 配列だけ」を渡すと 400 で弾く (全体 dict を要求)。"""
        nodes_json = json.dumps(BASE_PLAYBOOK["nodes"], ensure_ascii=False)
        with self.assertRaises(HTTPException) as ctx:
            _validate_playbook_data(
                BASE_PLAYBOOK["name"], "", nodes_json, _schema_json(BASE_PLAYBOOK)
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_name_description_overridden_by_args(self):
        """更新で name/description が変わるケース: 引数側が優先される。"""
        data = dict(BASE_PLAYBOOK)
        nodes_json = json.dumps(data, ensure_ascii=False)
        # nodes_json 内の name と異なる引数を渡しても検証は通る
        _validate_playbook_data(
            "renamed_pb", "new description", nodes_json, _schema_json(data)
        )

    def test_invalid_json_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            _validate_playbook_data("x", "", "{not valid json", "{}")
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
