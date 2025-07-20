import logging
import threading
import time
import subprocess
import sys
from pathlib import Path

import gradio as gr

from saiverse_manager import SAIVerseManager
from model_configs import get_model_choices

logging.basicConfig(level=logging.INFO)
manager = SAIVerseManager()
PERSONA_CHOICES = list(manager.persona_map.keys())

MODEL_CHOICES = get_model_choices()

NOTE_CSS = """
/* ① まず器（avatar-container）を拡大 */
#my_chat .avatar-container {
  width: 60px !important;
  max-width: 60px !important;
  height: 60px !important;
  min-width: 60px !important;   /* ← ここ大事：吹き出しの左余白確保 */
  min-height: 60px !important;
  border-radius: 20%;
  overflow: hidden;             /* はみ出しカット（object-fit と併用可） */
}

/* ② 中の <img> は「器いっぱい」に張り付け */
#my_chat .avatar-container img {
  width: 100% !important;       /* 96px に合わせて伸縮 */
  height: 100% !important;
  border-radius: 20%;
  padding: 0 !important;
  object-fit: cover;
}

#my_chat .note-box {
  background: #fff9db;
  color: #333350;
  border-left: 4px solid #ffbf00;
  padding: 8px 12px;
  margin: 0;
  border-radius: 6px;
  font-size: .92rem;
}
.note-box b {
  color: #333350; /* <b> の強調部分にも明示的に上書き */
}

.inline-avatar {
  width: 60px !important;
  height: 60px !important;
  max-width: 60px !important;
  max-height: 60px !important;
  float: left;
  margin: 0.5em !important;
  border-radius: 20%;
  object-fit: cover;
}

/* メッセージの高さがアイコンより低くならないよう調整 */
#my_chat .message {
  min-height: 60px;
  max-width: 768px;
  overflow: hidden;
  font-size: 1rem !important;
}
"""


def respond(message: str):
    """Process user input and return updated chat history."""
    manager.handle_user_input(message)
    history = manager.get_building_history("user_room")
    return history

def respond_stream(message: str):
    """Handle user input and return updated history."""
    manager.handle_user_input(message)
    final = manager.get_building_history("user_room")
    yield final


def call_persona(name: str):
    persona_id = manager.persona_map.get(name)
    if persona_id:
        manager.summon_persona(persona_id)
    return manager.get_building_history("user_room")


def select_model(model_name: str):
    manager.set_model(model_name)
    return manager.get_building_history("user_room")


def main():
    # --- API Serverを別プロセスで起動 ---
    api_server_path = Path(__file__).parent / "database" / "api_server.py"
    if api_server_path.exists():
        logging.info(f"Starting API Server from: {api_server_path}")
        subprocess.Popen([sys.executable, str(api_server_path)])
    else:
        logging.warning(f"API Server not found at {api_server_path}, skipping.")

    # --- DB Managerを別プロセスで起動 ---
    db_manager_path = Path(__file__).parent / "database" / "db_manager.py"
    if db_manager_path.exists():
        logging.info(f"Starting DB Manager from: {db_manager_path}")
        # Popenを使い、DB Managerのプロセスを待たずにメインアプリを続行する
        subprocess.Popen([sys.executable, str(db_manager_path)])
    else:
        logging.warning(f"DB Manager not found at {db_manager_path}, skipping.")

    def background_loop():
        persona_ids = list(manager.personas.keys())
        pulse_idx = 0
        while True:
            manager.run_scheduled_prompts()
            logging.debug("Background loop tick")
            now = time.time()
            if persona_ids and now >= manager.next_scheduled_pulse_time:
                pid = persona_ids[pulse_idx % len(persona_ids)]
                logging.info("Background loop running pulse for %s", pid)
                manager.run_pulse(pid)
                pulse_idx = (pulse_idx + 1) % len(persona_ids)
            time.sleep(5)

    thread = threading.Thread(target=background_loop, daemon=True)
    thread.start()

    with gr.Blocks(css=NOTE_CSS) as demo:
        chatbot = gr.Chatbot(
            type="messages",
            group_consecutive_messages=False,
            sanitize_html=False,
            elem_id="my_chat",
            avatar_images=(
                "assets/icons/user.png", # ← ユーザー
                None  # アシスタント側はメッセージ内に表示
            ),
            height=800
        )
        with gr.Row():
            with gr.Column(scale=4):
                txt = gr.Textbox(placeholder="ここにメッセージを入力...", lines=4)
            with gr.Column(scale=1):
                submit = gr.Button("送信")
        with gr.Row():
            model_drop = gr.Dropdown(choices=MODEL_CHOICES, value=MODEL_CHOICES[0], label="モデル選択")
            online_state = gr.State(True)
            online_btn = gr.Button("オンライン")
        with gr.Row():
            persona_drop = gr.Dropdown(choices=PERSONA_CHOICES, value=PERSONA_CHOICES[0], label="ペルソナ選択")
            call_btn = gr.Button("ペルソナを呼ぶ")
        submit.click(respond_stream, txt, chatbot)
        call_btn.click(call_persona, persona_drop, chatbot)
        model_drop.change(select_model, model_drop, chatbot)
        def toggle_online(state):
            new_state = not state
            manager.set_user_online(new_state)
            label = "オンライン" if new_state else "オフライン"
            return new_state, gr.update(value=label)

        online_btn.click(toggle_online, online_state, [online_state, online_btn])
    demo.launch(server_name="0.0.0.0", server_port=7860)


if __name__ == "__main__":
    main()
