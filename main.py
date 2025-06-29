import logging
import threading
import time

import gradio as gr

from saiverse_manager import SAIVerseManager

logging.basicConfig(level=logging.INFO)
manager = SAIVerseManager()
PERSONA_CHOICES = list(manager.persona_map.keys())

MODEL_CHOICES = [
    "gpt-4o",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "hf.co/unsloth/Qwen3-32B-GGUF:Q4_K_XL",
    "qwen3:30b",
    "llama4:16x17b",
    "hf.co/unsloth/gemma-3-27b-it-GGUF:Q6_K",
    "hf.co/unsloth/Mistral-Small-3.2-24B-Instruct-2506-GGUF:Q6_K_XL",
    "hf.co/mmnga/llm-jp-3.1-8x13b-instruct4-gguf:Q4_K_M",
    "hf.co/mmnga/ABEJA-Qwen2.5-32b-Japanese-v1.0-gguf:Q4_K_M"
]

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
    """Stream AI response for chat."""
    history = manager.get_building_history("user_room")
    history.append({"role": "user", "content": message})
    ai_message = ""
    for token in manager.handle_user_input_stream(message):
        ai_message += token
        yield history + [{"role": "assistant", "content": ai_message}]
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
    def background_loop():
        while True:
            manager.run_scheduled_prompts()
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
        with gr.Row():
            persona_drop = gr.Dropdown(choices=PERSONA_CHOICES, value=PERSONA_CHOICES[0], label="ペルソナ選択")
            call_btn = gr.Button("ペルソナを呼ぶ")
        submit.click(respond_stream, txt, chatbot)
        call_btn.click(call_persona, persona_drop, chatbot)
        model_drop.change(select_model, model_drop, chatbot)
    demo.launch(server_name="0.0.0.0", server_port=7860)


if __name__ == "__main__":
    main()

