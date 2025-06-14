import logging

import gradio as gr

from saiverse_manager import SAIVerseManager

logging.basicConfig(level=logging.INFO)
manager = SAIVerseManager()
PERSONA_CHOICES = list(manager.persona_map.keys())

NOTE_CSS = """
.note-box {
  background: #fff9db;
  color: #333350;
  border-left: 4px solid #ffbf00;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: .92rem;
}
.note-box b {
  color: #333350; /* <b> の強調部分にも明示的に上書き */
}
"""


def respond(message: str):
    """Process user input and return updated chat history."""
    manager.handle_user_input(message)
    history = manager.get_building_history("user_room")
    return history


def call_persona(name: str):
    persona_id = manager.persona_map.get(name)
    if persona_id:
        manager.summon_persona(persona_id)
    return manager.get_building_history("user_room")


def main():
    with gr.Blocks(css=NOTE_CSS) as demo:
        chatbot = gr.Chatbot(type="messages", group_consecutive_messages=False, sanitize_html=False, height=800)
        with gr.Row():
            txt = gr.Textbox()
            persona_drop = gr.Dropdown(choices=PERSONA_CHOICES, value=PERSONA_CHOICES[0], label="ペルソナ選択")
            call_btn = gr.Button("ペルソナを呼ぶ")
        txt.submit(respond, txt, chatbot)
        call_btn.click(call_persona, persona_drop, chatbot)
    demo.launch()


if __name__ == "__main__":
    main()

