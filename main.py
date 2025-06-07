import logging

import gradio as gr

from router import build_router

logging.basicConfig(level=logging.INFO)
router = build_router()


def respond(message, history):
    reply = router.handle_user_input(message)
    history = history or []
    history.append(("ユーザー", message))
    history.append(("AI", reply))
    return history, history


def main():
    with gr.Blocks() as demo:
        chatbot = gr.Chatbot()
        state = gr.State([])
        with gr.Row():
            txt = gr.Textbox()
        txt.submit(respond, [txt, state], [chatbot, state])
    demo.launch()


if __name__ == "__main__":
    main()

