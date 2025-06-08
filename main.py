import logging

import gradio as gr

from router import build_router

logging.basicConfig(level=logging.INFO)
router = build_router()


def respond(message, history):
    replies = router.handle_user_input(message)
    history = history or []
    if message:
        history.append({"role": "user", "content": message})
    for rep in replies:
        history.append({"role": "assistant", "content": rep})
    return history, history


def main():
    with gr.Blocks() as demo:
        chatbot = gr.Chatbot(type="messages")
        state = gr.State([])
        with gr.Row():
            txt = gr.Textbox()
        txt.submit(respond, [txt, state], [chatbot, state])
    demo.launch()


if __name__ == "__main__":
    main()

