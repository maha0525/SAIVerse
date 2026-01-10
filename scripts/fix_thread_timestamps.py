#!/usr/bin/env python3
"""
タイムスタンプ補正スクリプト

SAIMemoryのスレッド内メッセージのタイムスタンプを、開始日時と終了日時の間で
均等に再配置します。時系列の順序は保持されます。

Usage:
    python scripts/fix_thread_timestamps.py <persona_id> <thread_id> <start_datetime> <end_datetime>

Examples:
    # 2024年1月1日 9:00 から 2024年1月1日 18:00 の間に均等配置
    python scripts/fix_thread_timestamps.py air_city_a "air_city_a:__persona__" "2024-01-01 09:00:00" "2024-01-01 18:00:00"
    
    # ISO 8601形式でも指定可能
    python scripts/fix_thread_timestamps.py nagi_city_a "nagi_city_a:main" "2024-03-15T14:30:00" "2024-03-15T22:00:00"
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))


def get_db_path(persona_id: str) -> Path:
    """ペルソナのメモリDBパスを取得"""
    return Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"


def parse_datetime(dt_str: str) -> datetime:
    """文字列を datetime に変換（複数フォーマット対応）"""
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            continue
    raise ValueError(
        f"日時の形式が不正です: {dt_str}\n"
        "対応形式: 'YYYY-MM-DD HH:MM:SS' または 'YYYY-MM-DDTHH:MM:SS'"
    )


def get_thread_messages(conn: sqlite3.Connection, thread_id: str) -> List[Tuple[str, int]]:
    """スレッド内のメッセージを取得（ID, 現在のタイムスタンプ順）"""
    cur = conn.execute(
        """
        SELECT id, created_at FROM messages 
        WHERE thread_id = ? 
        ORDER BY created_at ASC, rowid ASC
        """,
        (thread_id,)
    )
    return cur.fetchall()


def preview_changes(
    messages: List[Tuple[str, int]], 
    new_timestamps: List[int]
) -> None:
    """変更のプレビューを表示"""
    print("\n--- 変更プレビュー ---")
    print(f"対象メッセージ数: {len(messages)}")
    
    if len(messages) <= 10:
        for i, ((msg_id, old_ts), new_ts) in enumerate(zip(messages, new_timestamps)):
            old_dt = datetime.fromtimestamp(old_ts).strftime("%Y-%m-%d %H:%M:%S")
            new_dt = datetime.fromtimestamp(new_ts).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  [{i+1:3d}] {msg_id[:8]}... : {old_dt} -> {new_dt}")
    else:
        # 最初と最後の5件だけ表示
        for i in range(5):
            msg_id, old_ts = messages[i]
            new_ts = new_timestamps[i]
            old_dt = datetime.fromtimestamp(old_ts).strftime("%Y-%m-%d %H:%M:%S")
            new_dt = datetime.fromtimestamp(new_ts).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  [{i+1:3d}] {msg_id[:8]}... : {old_dt} -> {new_dt}")
        
        print(f"  ... (中略: {len(messages) - 10} 件) ...")
        
        for i in range(len(messages) - 5, len(messages)):
            msg_id, old_ts = messages[i]
            new_ts = new_timestamps[i]
            old_dt = datetime.fromtimestamp(old_ts).strftime("%Y-%m-%d %H:%M:%S")
            new_dt = datetime.fromtimestamp(new_ts).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  [{i+1:3d}] {msg_id[:8]}... : {old_dt} -> {new_dt}")


def apply_timestamps(
    conn: sqlite3.Connection, 
    messages: List[Tuple[str, int]], 
    new_timestamps: List[int]
) -> int:
    """新しいタイムスタンプを適用"""
    updated = 0
    for (msg_id, _), new_ts in zip(messages, new_timestamps):
        conn.execute(
            "UPDATE messages SET created_at = ? WHERE id = ?",
            (new_ts, msg_id)
        )
        updated += 1
    conn.commit()
    return updated


def main():
    parser = argparse.ArgumentParser(
        description="スレッド内メッセージのタイムスタンプを均等に再配置します",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("persona_id", help="ペルソナID (例: air_city_a)")
    parser.add_argument("thread_id", help="スレッドID (例: air_city_a:__persona__)")
    parser.add_argument("start_datetime", help="開始日時 (例: '2024-01-01 09:00:00')")
    parser.add_argument("end_datetime", help="終了日時 (例: '2024-01-01 18:00:00')")
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="変更を適用せずプレビューのみ表示"
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="確認をスキップして即座に適用"
    )
    
    args = parser.parse_args()
    
    # DBパス確認
    db_path = get_db_path(args.persona_id)
    if not db_path.exists():
        print(f"エラー: データベースが見つかりません: {db_path}", file=sys.stderr)
        sys.exit(1)
    
    # 日時パース
    try:
        start_dt = parse_datetime(args.start_datetime)
        end_dt = parse_datetime(args.end_datetime)
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
    
    if start_dt >= end_dt:
        print("エラー: 開始日時は終了日時より前である必要があります", file=sys.stderr)
        sys.exit(1)
    
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())
    
    print(f"ペルソナ: {args.persona_id}")
    print(f"スレッド: {args.thread_id}")
    print(f"期間: {start_dt} ~ {end_dt}")
    
    # DB接続
    conn = sqlite3.connect(db_path)
    
    try:
        # メッセージ取得
        messages = get_thread_messages(conn, args.thread_id)
        
        if not messages:
            print("エラー: 指定されたスレッドにメッセージがありません", file=sys.stderr)
            sys.exit(1)
        
        # 新しいタイムスタンプを計算（均等配置）
        if len(messages) == 1:
            # 1件の場合は開始日時を使用
            new_timestamps = [start_ts]
        else:
            # 複数件の場合は均等に配置
            interval = (end_ts - start_ts) / (len(messages) - 1)
            new_timestamps = [
                int(start_ts + i * interval) 
                for i in range(len(messages))
            ]
        
        # プレビュー表示
        preview_changes(messages, new_timestamps)
        
        if args.dry_run:
            print("\n(ドライラン: 変更は適用されません)")
            return
        
        # 確認
        if not args.yes:
            print()
            response = input("この変更を適用しますか? [y/N]: ").strip().lower()
            if response not in ("y", "yes"):
                print("キャンセルしました")
                return
        
        # 適用
        updated = apply_timestamps(conn, messages, new_timestamps)
        print(f"\n✓ {updated} 件のメッセージのタイムスタンプを更新しました")
        
    finally:
        conn.close()


if __name__ == "__main__":
    main()
