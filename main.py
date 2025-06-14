import logging

import gradio as gr

from router import build_router

logging.basicConfig(level=logging.INFO)
router = build_router()

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
    router.handle_user_input(message)
    history = router.get_building_history("user_room")
    return history


def call_air():
    """Summon Air to the user room."""
    router.summon_air()
    return router.get_building_history("user_room")


def main():
    with gr.Blocks(css=NOTE_CSS) as demo:
        chatbot = gr.Chatbot(type="messages", group_consecutive_messages=False, sanitize_html=False, height=800)
        with gr.Row():
            txt = gr.Textbox()
            call_btn = gr.Button("エアを呼ぶ")
        txt.submit(respond, txt, chatbot)
        call_btn.click(call_air, None, chatbot)
    demo.launch()


if __name__ == "__main__":
    main()

