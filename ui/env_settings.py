from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import gradio as gr


ENV_FILE_PATH = Path(".env")

# センシティブな環境変数のキーワード
SENSITIVE_KEYWORDS = ["KEY", "TOKEN", "SECRET", "PASSWORD"]


def is_sensitive_key(key: str) -> bool:
    """キーがセンシティブかどうかを判定"""
    return any(keyword in key.upper() for keyword in SENSITIVE_KEYWORDS)


def read_env_file() -> List[Tuple[str, str, str]]:
    """
    .envファイルを読み込んで、(key, value, original_line)のリストを返す
    コメント行や空行は保持する
    """
    if not ENV_FILE_PATH.exists():
        return []

    result = []
    with open(ENV_FILE_PATH, "r", encoding="utf-8") as f:
        for line in f:
            original = line.rstrip("\n")
            stripped = line.strip()

            # コメント行や空行
            if not stripped or stripped.startswith("#"):
                result.append(("", "", original))
                continue

            # KEY=VALUE形式をパース
            match = re.match(r'^([^=]+)=(.*)$', stripped)
            if match:
                key = match.group(1).strip()
                value = match.group(2).strip()
                # クォート除去
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                result.append((key, value, original))
            else:
                result.append(("", "", original))

    return result


def parse_env_to_dict() -> Dict[str, str]:
    """環境変数をkey=valueの辞書として返す（コメント行は除外）"""
    env_data = read_env_file()
    return {key: value for key, value, _ in env_data if key}


def save_env_dict(env_dict: Dict[str, str]) -> str:
    """
    環境変数の辞書を.envファイルに保存する
    既存のコメント行や空行は保持し、値のみ更新する
    """
    try:
        env_data = read_env_file()
        new_lines = []
        updated_keys = set()

        for key, value, original in env_data:
            if not key:
                # コメント行や空行はそのまま保持
                new_lines.append(original)
            elif key in env_dict:
                # 値を更新
                new_value = env_dict[key]
                # クォートで囲む（値にスペースが含まれる場合）
                if " " in new_value or "=" in new_value:
                    new_lines.append(f'{key}="{new_value}"')
                else:
                    new_lines.append(f"{key}={new_value}")
                updated_keys.add(key)
            else:
                # 辞書にないキーはそのまま保持
                new_lines.append(original)

        # 新しく追加されたキー
        for key, value in env_dict.items():
            if key not in updated_keys:
                if " " in value or "=" in value:
                    new_lines.append(f'{key}="{value}"')
                else:
                    new_lines.append(f"{key}={value}")

        # ファイルに書き込み
        with open(ENV_FILE_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines))
            if new_lines:
                f.write("\n")  # 末尾に改行

        return "✅ 環境変数を保存しました。変更を反映するには、下の「サーバーを再起動」ボタンをクリックしてください。"
    except Exception as e:
        return f"❌ エラー: 環境変数の保存に失敗しました。{str(e)}"


def get_env_display() -> str:
    """環境変数を表示用にフォーマットする（APIキーは伏せ字）"""
    env_data = read_env_file()
    lines = []

    for key, value, original in env_data:
        if not key:
            lines.append(original)
        else:
            # センシティブなキーは伏せ字
            if is_sensitive_key(key) and value:
                masked_value = value[:4] + "..." + value[-4:] if len(value) > 8 else "***"
                lines.append(f"{key}={masked_value}")
            else:
                lines.append(f"{key}={value}")

    return "\n".join(lines)


def reload_env_display():
    """環境変数の表示を再読み込み"""
    return get_env_display()


def restart_server():
    """
    サーバーを再起動する
    現在のプロセスを新しいプロセスで置き換える
    """
    try:
        # 現在の実行引数を取得
        python_executable = sys.executable
        script_args = sys.argv

        # プロセスを再起動
        os.execv(python_executable, [python_executable] + script_args)
    except Exception as e:
        return f"❌ エラー: サーバーの再起動に失敗しました。{str(e)}\n\n手動で再起動してください。"


def create_env_settings_ui():
    """環境設定UIを作成"""
    gr.Markdown("## 環境設定")
    gr.Markdown(
        """
        SAIVerseの環境変数を管理します。APIキーや設定値を編集できます。

        **⚠️ 注意**: 環境変数を変更した後は、サーバーの再起動が必要です。
        """
    )

    with gr.Tabs():
        with gr.TabItem("表示"):
            env_display = gr.Textbox(
                value=get_env_display,
                label="現在の環境変数（APIキーは伏せ字表示）",
                interactive=False,
                lines=20,
                max_lines=30
            )
            refresh_btn = gr.Button("🔄 再読み込み", variant="secondary")
            refresh_btn.click(fn=reload_env_display, inputs=None, outputs=env_display)

        with gr.TabItem("編集"):
            gr.Markdown(
                """
                ### 個別編集
                各環境変数を個別に編集できます。センシティブな値（APIキーなど）はパスワードフィールドで保護されています。

                **編集方法:**
                - 値を変更したい項目に新しい値を入力してください
                - センシティブな値を変更しない場合は、空白のままにしてください
                - 保存後、サーバーを再起動すると変更が反映されます
                """
            )

            # 環境変数を読み込む
            env_dict = parse_env_to_dict()

            # 入力フィールドのリスト
            input_components = {}

            with gr.Column():
                for key in sorted(env_dict.keys()):
                    value = env_dict[key]

                    if is_sensitive_key(key):
                        # センシティブキーはパスワードフィールド
                        input_components[key] = gr.Textbox(
                            label=f"🔒 {key}",
                            value="",  # デフォルトは空（変更なし）
                            type="password",
                            placeholder="新しい値を入力（空白のままで変更なし）",
                            info=f"現在の値: {'*' * min(len(value), 20)}" if value else "（未設定）"
                        )
                    else:
                        # 通常のキーは平文フィールド
                        input_components[key] = gr.Textbox(
                            label=key,
                            value=value,
                            placeholder="値を入力"
                        )

            def save_edited_env(*values):
                """編集された環境変数を保存"""
                # 変更された値のみを辞書に格納
                updated_env = {}
                for i, key in enumerate(sorted(env_dict.keys())):
                    new_value = values[i] if i < len(values) else ""

                    if is_sensitive_key(key):
                        # センシティブキーは空でなければ更新
                        if new_value:
                            updated_env[key] = new_value
                        else:
                            # 既存の値を保持
                            updated_env[key] = env_dict[key]
                    else:
                        # 通常のキーは常に更新
                        updated_env[key] = new_value

                return save_env_dict(updated_env)

            save_btn = gr.Button("💾 保存", variant="primary")
            save_status = gr.Textbox(label="ステータス", interactive=False)

            # 保存ボタンのクリックイベント
            save_btn.click(
                fn=save_edited_env,
                inputs=list(input_components.values()),
                outputs=save_status
            )

    gr.Markdown("---")
    gr.Markdown("### サーバー再起動")
    gr.Markdown(
        """
        環境変数を変更した場合、サーバーを再起動する必要があります。

        **自動再起動**: 下のボタンをクリックすると、サーバーが自動的に再起動します。

        **⚠️ 注意**:
        - 再起動中は一時的にUIにアクセスできなくなります（数秒程度）
        - ブラウザで自動的にリロードされない場合は、手動でリロードしてください
        """
    )

    with gr.Row():
        restart_btn = gr.Button("🔄 サーバーを再起動", variant="primary")
        restart_status = gr.Textbox(label="再起動ステータス", interactive=False, visible=False)

    gr.Markdown(
        """
        **手動再起動の手順** （自動再起動が動作しない場合）:
        1. ターミナルで `Ctrl+C` を押してサーバーを停止
        2. `python main.py city_a` を実行してサーバーを再起動
        """
    )

    restart_btn.click(fn=restart_server, inputs=None, outputs=restart_status)
