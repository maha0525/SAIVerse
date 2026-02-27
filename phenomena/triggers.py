"""
phenomena.triggers ― トリガーイベント定義

SAIVerseで発生する各種イベントをトリガーとして定義し、
フェノメノンの発火条件として使用する。
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class TriggerType(str, Enum):
    """トリガーの種類"""
    SERVER_START = "server_start"       # サーバー起動時
    SERVER_STOP = "server_stop"         # サーバー終了時
    USER_SPEECH = "user_speech"         # ユーザー発話時
    PERSONA_SPEECH = "persona_speech"   # ペルソナ発話時
    PERSONA_MOVE = "persona_move"       # ペルソナ移動時
    USER_MOVE = "user_move"             # ユーザー移動時
    USER_LOGIN = "user_login"           # ユーザーログイン時
    USER_LOGOUT = "user_logout"         # ユーザーログアウト時
    SCHEDULE_FIRED = "schedule_fired"   # スケジュール発火時
    # 外部イベントトリガー
    X_MENTION_RECEIVED = "x_mention_received"  # Xメンション受信時
    EXTERNAL_WEBHOOK = "external_webhook"      # 汎用Webhook受信時


# 各トリガータイプのデータスキーマ定義
TRIGGER_SCHEMAS: Dict[TriggerType, Dict[str, str]] = {
    TriggerType.SERVER_START: {
        "city_id": "サーバーのCity ID",
    },
    TriggerType.SERVER_STOP: {
        "city_id": "サーバーのCity ID",
    },
    TriggerType.USER_SPEECH: {
        "building_id": "発話が行われた建物ID",
        "content": "発話内容",
    },
    TriggerType.PERSONA_SPEECH: {
        "persona_id": "発話したペルソナID",
        "building_id": "発話が行われた建物ID",
        "content": "発話内容",
    },
    TriggerType.PERSONA_MOVE: {
        "persona_id": "移動したペルソナID",
        "from_building": "移動元の建物ID",
        "to_building": "移動先の建物ID",
    },
    TriggerType.USER_MOVE: {
        "from_building": "移動元の建物ID",
        "to_building": "移動先の建物ID",
    },
    TriggerType.USER_LOGIN: {
        "building_id": "ログイン時の建物ID",
    },
    TriggerType.USER_LOGOUT: {
        "last_building_id": "最後にいた建物ID",
    },
    TriggerType.SCHEDULE_FIRED: {
        "schedule_id": "発火したスケジュールID",
        "persona_id": "対象のペルソナID",
    },
    TriggerType.X_MENTION_RECEIVED: {
        "persona_id": "対象のペルソナID",
        "tweet_id": "メンション元のツイートID",
        "author_username": "メンション送信者のユーザー名",
        "author_name": "メンション送信者の表示名",
        "mention_text": "メンション本文",
        "playbook_params_json": "Playbook実行パラメータ（JSON文字列）",
    },
    TriggerType.EXTERNAL_WEBHOOK: {
        "source": "Webhookの送信元",
        "payload": "Webhookのペイロード（JSON文字列）",
    },
}


@dataclass
class TriggerEvent:
    """トリガーイベントを表すデータクラス"""
    type: TriggerType
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[str] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()

    def get(self, key: str, default: Any = None) -> Any:
        """データフィールドへの便利なアクセサ"""
        return self.data.get(key, default)

    def __repr__(self) -> str:
        return f"TriggerEvent(type={self.type.value}, data={self.data})"
