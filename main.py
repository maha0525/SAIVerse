import logging

import gradio as gr

from router import build_router

logging.basicConfig(level=logging.INFO)
router = build_router()


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
    with gr.Blocks() as demo:
        chatbot = gr.Chatbot(type="messages", height=600)
        with gr.Row():
            txt = gr.Textbox()
            call_btn = gr.Button("エアを呼ぶ")
        txt.submit(respond, txt, chatbot)
        call_btn.click(call_air, None, chatbot)
    demo.launch()


if __name__ == "__main__":
    main()

