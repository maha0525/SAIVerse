"""Track 種別ごとのイベント振る舞い Handler 群。

TrackManager は全 Track の汎用管理に専念し、種別ごとの「どのイベントで
どう反応するか」のロジックは本パッケージの Handler クラスが担う。

新しい Track 種別を追加する際は、既存の Handler を変更せず、新しい
Handler クラスを本パッケージに追加して呼び出し元 (handle_user_input
相当の入口) から呼び出す形で拡張する。
"""

from .social_track_handler import SocialTrackHandler

__all__ = [
    "SocialTrackHandler",
]
