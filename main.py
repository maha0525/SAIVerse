import logging

import gradio as gr

from router import build_router

logging.basicConfig(level=logging.INFO)
router = build_router()


def respond(message, _):
    router.handle_user_input(message)
    history = router.get_building_history("user_room")
    return history, history


def main():
    with gr.Blocks() as demo:
        chatbot = gr.Chatbot(type="messages", height=600)
        with gr.Row():
            txt = gr.Textbox()
        txt.submit(respond, [txt, None], [chatbot, None])
    demo.launch()


if __name__ == "__main__":
    main()

