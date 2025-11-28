from __future__ import annotations

from functools import partial

import gradio as gr

from database.db_manager import create_db_manager_ui
from tools.utilities.memory_settings_ui import create_memory_settings_ui
from ui import state as ui_state
from ui.env_settings import create_env_settings_ui
from ui.chat import (
    call_persona_ui,
    end_conversation_ui,
    format_location_label,
    get_autonomous_log,
    get_current_building_history,
    get_current_location_name,
    login_ui,
    logout_ui,
    move_user_radio_ui,
    move_user_ui,
    respond_stream,
    select_model,
    start_conversations_ui,
    stop_conversations_ui,
    go_to_user_room_ui,
    update_model_parameter,
)
from ui.world_editor import create_world_editor_ui
from ui.task_manager import create_task_manager_ui


def _require_manager():
    manager = ui_state.manager
    if manager is None:
        raise RuntimeError("Manager not initialised")
    return manager


def build_app(city_name: str, note_css: str, head_viewport: str):
    manager = _require_manager()

    with gr.Blocks(fill_width=True, head=head_viewport, css=note_css, title=f"SAIVerse City: {city_name}", theme=gr.themes.Soft()) as demo:
        with gr.Sidebar(open=False, width=340, elem_id="sample_sidebar", elem_classes=["saiverse-sidebar"]):
            with gr.Accordion("セクション切り替え", open=True):
                gr.HTML("""
                    <div id="saiverse-sidebar-nav">
                        <div class="saiverse-nav-item" data-tab-label="ホーム">ホーム</div>
                        <div class="saiverse-nav-item" data-tab-label="ワールドビュー">ワールドビュー</div>
                        <div class="saiverse-nav-item" data-tab-label="自律会話ログ" style="display:none">自律会話ログ</div>
                        <div class="saiverse-nav-item" data-tab-label="DB Manager">DB Manager</div>
                        <div class="saiverse-nav-item" data-tab-label="タスクマネージャー">タスクマネージャー</div>
                        <div class="saiverse-nav-item" data-tab-label="メモリー設定">メモリー設定</div>
                        <div class="saiverse-nav-item" data-tab-label="ワールドエディタ">ワールドエディタ</div>
                        <div class="saiverse-nav-item" data-tab-label="環境設定">⚙️ 環境設定</div>
                    </div>
                    """)
            with gr.Row():
                login_status_display = gr.Textbox(
                    value="オンライン" if manager.user_is_online else "オフライン",
                    label="ログイン状態",
                    interactive=False,
                    scale=1
                )
                login_btn = gr.Button("ログイン", scale=1)
                logout_btn = gr.Button("ログアウト", scale=1)
            with gr.Accordion("自律会話管理", open=False):
                with gr.Column(elem_classes=["saiverse-sidebar-autolog-controls"]):
                    start_button = gr.Button("自律会話を開始", variant="primary", scale=1)
                    stop_button = gr.Button("自律会話を停止", variant="stop", scale=1)
                    status_display = gr.Textbox(
                        value="停止中",
                        label="現在のステータス",
                        interactive=False,
                        scale=1
                    )
            with gr.Accordion("ネットワークモード", open=False):
                with gr.Row():
                    sds_status_display = gr.Textbox(
                        value=manager.sds_status,
                        interactive=False,
                        scale=2,
                        show_label=False
                    )
                    online_btn = gr.Button("オンラインモードへ", scale=1)
                    offline_btn = gr.Button("オフラインモードへ", scale=1)
            with gr.Accordion("移動", open=True):
                move_destination_radio = gr.Radio(
                    choices=ui_state.building_choices,
                    value=None,
                    label="移動先",
                    interactive=True,
                    elem_classes=["saiverse-move-radio"],
                    show_label=False
                )
        with gr.Column(elem_id="section-home", elem_classes=['saiverse-section', 'saiverse-home']):
            gr.Markdown(
                f"""
                ## ようこそ、{city_name} へ

                ここではワールドビューを開く前にゆっくり準備できるよ。左のラジオで建物を選んでからワールドビューへ進もう。
                """
            )
            gr.Markdown(
                """
                - ワールドビューを開くときに長い履歴を読み込む場合があるから、ここで一息ついてから進もう。
                - DB Manager やタスクマネージャーには直接ジャンプできるよ。
                """
            )
            enter_worldview_btn = gr.Button("ワールドビューに入る", variant="primary", elem_id="enter_worldview_btn")

        with gr.Column(elem_id="section-worldview", elem_classes=['saiverse-section', 'saiverse-hidden']):
            with gr.Row(elem_id="chat_header"):
                user_location_display = gr.Textbox(
                    # managerから現在地を取得して表示する
                    value=lambda: manager.building_map.get(manager.user_current_building_id).name if manager.user_current_building_id and manager.user_current_building_id in manager.building_map else "不明な場所",
                    label="あなたの現在地",
                    interactive=False,
                    scale=2,
                    visible=False
                )
                move_building_dropdown = gr.Dropdown(
                    choices=ui_state.building_choices,
                    label="移動先の建物",
                    interactive=True,
                    scale=2,
                    visible=False
                )
                move_btn = gr.Button("移動", scale=1, visible=False)

                current_location_display = gr.Markdown(
                    value=lambda: format_location_label(get_current_location_name())
                )
            with gr.Group(elem_id="chat_scroll_area"):
                chatbot = gr.Chatbot(
                    type="messages",
                    value=[],
                    group_consecutive_messages=False,
                    sanitize_html=False,
                    elem_id="my_chat",
                    avatar_images=(None, None),
                    autoscroll=True,
                    show_label=False
                )
            with gr.Group(elem_id="composer_fixed"):
                with gr.Row():
                    with gr.Column(scale=5):
                        with gr.Row(elem_id="message_input_row", equal_height=True):
                            txt = gr.Textbox(
                                placeholder="ここにメッセージを入力...",
                                lines=3,
                                elem_id="chat_message_textbox",
                                show_label=False
                            )
                    with gr.Column(scale=1, min_width=0):
                        submit = gr.Button(value="↑",variant="primary")
                        image_input = gr.UploadButton(
                            "📎",
                            file_types=["image"],
                            file_count="single",
                            elem_id="attachment_button"
                        )
                with gr.Accordion("オプション", open=False):
                    model_drop = gr.Dropdown(choices=ui_state.model_choices, value="None",label="モデル選択")
                    with gr.Row():
                        temperature_slider = gr.Slider(
                            minimum=0,
                            maximum=2,
                            step=0.1,
                            label="temperature",
                            visible=False,
                            interactive=True,
                        )
                        top_p_slider = gr.Slider(
                            minimum=0,
                            maximum=1,
                            step=0.05,
                            label="top_p",
                            visible=False,
                            interactive=True,
                        )
                    with gr.Row():
                        max_tokens_number = gr.Number(
                            label="max_completion_tokens",
                            visible=False,
                            precision=0,
                            interactive=True,
                        )
                        reasoning_dropdown = gr.Dropdown(
                            label="reasoning_effort",
                            choices=[],
                            visible=False,
                            interactive=True,
                        )
                        verbosity_dropdown = gr.Dropdown(
                            label="verbosity",
                            choices=[],
                            visible=False,
                            interactive=True,
                        )
                    refresh_chat_btn = gr.Button("履歴を再読み込み", variant="secondary", elem_id="refresh_chat_btn")
                    with gr.Row():
                        with gr.Column():
                            summon_persona_dropdown = gr.Dropdown(
                                choices=manager.get_summonable_personas(),
                                label="呼ぶペルソナを選択",
                                interactive=True,
                                scale=3
                            )
                            summon_btn = gr.Button("呼ぶ", scale=1)
                        with gr.Column():
                            end_conv_persona_dropdown = gr.Dropdown(
                                choices=manager.get_conversing_personas(),
                                label="帰ってもらうペルソナを選択",
                                interactive=True,
                                scale=3
                            )
                            end_conv_btn = gr.Button("帰宅", scale=1)

            client_location_state = gr.State()
            parameter_state = gr.State({})

            # --- Event Handlers ---
            submit.click(
                respond_stream,
                [txt, image_input],
                [chatbot, move_building_dropdown, move_destination_radio, summon_persona_dropdown, end_conv_persona_dropdown, image_input],
            )
            txt.submit(
                respond_stream,
                [txt, image_input],
                [chatbot, move_building_dropdown, move_destination_radio, summon_persona_dropdown, end_conv_persona_dropdown, image_input],
            )  # Enter key submission
            move_btn.click(
                fn=move_user_ui,
                inputs=[move_building_dropdown, client_location_state],
                outputs=[
                    chatbot,
                    user_location_display,
                    current_location_display,
                    move_building_dropdown,
                    move_destination_radio,
                    summon_persona_dropdown,
                    end_conv_persona_dropdown,
                    client_location_state,
                ],
            )
            move_radio_event = move_destination_radio.change(
                fn=move_user_radio_ui,
                inputs=[move_destination_radio, client_location_state],
                outputs=[
                    move_building_dropdown,
                    chatbot,
                    user_location_display,
                    current_location_display,
                    move_destination_radio,
                    summon_persona_dropdown,
                    end_conv_persona_dropdown,
                    client_location_state,
                ],
                show_progress="hidden",
                js="""
                (value) => {
                    const ensureSession = () => {
                        if (!window.saiverseSessionId) {
                            try {
                                window.saiverseSessionId = crypto.randomUUID();
                            } catch (err) {
                                window.saiverseSessionId = 'js-' + Math.random().toString(16).slice(2);
                            }
                        }
                        return window.saiverseSessionId;
                    };
                    const session = ensureSession();
                    console.debug('[ui-js] radio change value=%s session=%s', value, session);
                    if (value) {
                        window.saiverseNextBuilding = value;
                    }
                    return [value, null, null, null, null, null, null, {session}];
                }
                """
            )
            move_radio_event.then(
                None,
                None,
                None,
                js="""
                () => {
                    if (window.saiverseActiveSection !== "ワールドビュー") {
                        const navItem = document.querySelector('#saiverse-sidebar-nav .saiverse-nav-item[data-tab-label="ワールドビュー"]');
                        if (navItem) {
                            window.saiverseAutoLoadEnabled = true;
                            console.debug('[ui-js] switching to worldview for selection=%s', window.saiverseNextBuilding);
                            navItem.click();
                        }
                    } else if (window.saiverseTriggerWorldviewLoad) {
                        window.saiverseAutoLoadEnabled = true;
                        window.saiverseWorldviewPending = true;
                        window.saiverseTriggerWorldviewLoad();
                    }
                }
                """
            )
            enter_worldview_btn.click(
                fn=go_to_user_room_ui,
                inputs=[client_location_state],
                outputs=[
                    chatbot,
                    user_location_display,
                    current_location_display,
                    move_building_dropdown,
                    move_destination_radio,
                    summon_persona_dropdown,
                    end_conv_persona_dropdown,
                    client_location_state,
                ],
            )
            summon_btn.click(fn=call_persona_ui, inputs=[summon_persona_dropdown], outputs=[chatbot, summon_persona_dropdown, end_conv_persona_dropdown])
            refresh_chat_btn.click(
                fn=get_current_building_history,
                inputs=None,
                outputs=chatbot,
                show_progress="hidden",
            )
            login_btn.click(
                fn=login_ui,
                inputs=None,
                outputs=[login_status_display, summon_persona_dropdown, end_conv_persona_dropdown]
            )
            logout_btn.click(fn=logout_ui, inputs=None, outputs=login_status_display)
            model_drop.change(
                select_model,
                [model_drop],
                [
                    chatbot,
                    temperature_slider,
                    top_p_slider,
                    max_tokens_number,
                    reasoning_dropdown,
                    verbosity_dropdown,
                    parameter_state,
                ],
            )
            temperature_slider.change(
                partial(update_model_parameter, "temperature"),
                [temperature_slider, parameter_state, model_drop],
                parameter_state,
            )
            top_p_slider.change(
                partial(update_model_parameter, "top_p"),
                [top_p_slider, parameter_state, model_drop],
                parameter_state,
            )
            max_tokens_number.change(
                partial(update_model_parameter, "max_completion_tokens"),
                [max_tokens_number, parameter_state, model_drop],
                parameter_state,
            )
            reasoning_dropdown.change(
                partial(update_model_parameter, "reasoning_effort"),
                [reasoning_dropdown, parameter_state, model_drop],
                parameter_state,
            )
            verbosity_dropdown.change(
                partial(update_model_parameter, "verbosity"),
                [verbosity_dropdown, parameter_state, model_drop],
                parameter_state,
            )
            online_btn.click(fn=manager.switch_to_online_mode, inputs=None, outputs=sds_status_display)
            offline_btn.click(fn=manager.switch_to_offline_mode, inputs=None, outputs=sds_status_display)
            end_conv_btn.click(
                fn=end_conversation_ui,
                inputs=[end_conv_persona_dropdown],
                outputs=[chatbot, summon_persona_dropdown, end_conv_persona_dropdown]
            )


        with gr.Column(elem_id="section-autolog", elem_classes=['saiverse-section', 'saiverse-hidden']):
            with gr.Row():
                log_building_dropdown = gr.Dropdown(
                    choices=ui_state.autonomous_building_choices,
                    value=ui_state.autonomous_building_choices[0] if ui_state.autonomous_building_choices else None,
                    label="Building選択",
                    interactive=bool(ui_state.autonomous_building_choices)
                )
                log_refresh_btn = gr.Button("手動更新")
            log_chatbot = gr.Chatbot(
                type="messages",
                group_consecutive_messages=False,
                sanitize_html=False,
                elem_id="log_chat",
                height=800
            )
            # JavaScriptからクリックされるための、非表示の自動更新ボタン
            auto_refresh_log_btn = gr.Button("Auto-Refresh Trigger", visible=False, elem_id="auto_refresh_log_btn")

            # イベントハンドラ (ON/OFF)
            start_button.click(fn=start_conversations_ui, inputs=None, outputs=status_display)
            stop_button.click(fn=stop_conversations_ui, inputs=None, outputs=status_display)

            # イベントハンドラ
            log_building_dropdown.change(fn=get_autonomous_log, inputs=log_building_dropdown, outputs=log_chatbot, show_progress="hidden")
            log_refresh_btn.click(fn=get_autonomous_log, inputs=log_building_dropdown, outputs=log_chatbot, show_progress="hidden")
            auto_refresh_log_btn.click(fn=get_autonomous_log, inputs=log_building_dropdown, outputs=log_chatbot, show_progress="hidden")


        with gr.Column(elem_id="section-db-manager", elem_classes=['saiverse-section', 'saiverse-hidden']):
            create_db_manager_ui(manager.SessionLocal)

        with gr.Column(elem_id="section-task-manager", elem_classes=['saiverse-section', 'saiverse-hidden']):
            create_task_manager_ui(manager)

        with gr.Column(elem_id="section-memory-settings", elem_classes=['saiverse-section', 'saiverse-hidden']):
            create_memory_settings_ui(manager)


        with gr.Column(elem_id="section-world-editor", elem_classes=['saiverse-section', 'saiverse-hidden']):
            create_world_editor_ui() # This function now contains all editor sections

        with gr.Column(elem_id="section-env-settings", elem_classes=['saiverse-section', 'saiverse-hidden']):
            create_env_settings_ui()


        # UIロード時にJavaScriptを実行し、5秒ごとの自動更新タイマーを設定する
        js_auto_refresh = """
        () => {
            const sections = {
                "ホーム": "#section-home",
                "ワールドビュー": "#section-worldview",
                "自律会話ログ": "#section-autolog",
                "DB Manager": "#section-db-manager",
                "タスクマネージャー": "#section-task-manager",
                "メモリー設定": "#section-memory-settings",
                "ワールドエディタ": "#section-world-editor",
                "環境設定": "#section-env-settings"
            };
            const defaultLabel = "ホーム";
            window.saiverseActiveSection = defaultLabel;
            window.saiverseWorldviewInitialized = false;
            window.saiverseWorldviewPending = false;
            window.saiverseAutoLoadEnabled = window.saiverseAutoLoadEnabled ?? false;
            window.saiverseWorldEditorInitialized = window.saiverseWorldEditorInitialized ?? false;
            window.saiverseWorldEditorPending = false;
            const triggerWorldviewLoad = () => {
                if (!window.saiverseWorldviewPending) {
                    return;
                }
                if (!window.saiverseAutoLoadEnabled) {
                    return;
                }
                const button = document.querySelector("#refresh_chat_btn button, #refresh_chat_btn");
                if (button) {
                    window.saiverseWorldviewInitialized = true;
                    window.saiverseWorldviewPending = false;

                    // 一度非表示にしてから表示することでGradioのautoscrollを発動させる
                    const worldviewSection = document.querySelector("#section-worldview");
                    if (worldviewSection) {
                        worldviewSection.classList.add("saiverse-hidden");
                        button.click();
                        setTimeout(() => {
                            worldviewSection.classList.remove("saiverse-hidden");
                        }, 50);
                    } else {
                        button.click();
                    }
                } else {
                    requestAnimationFrame(triggerWorldviewLoad);
                }
            };
            const triggerWorldEditorLoad = () => {
                if (!window.saiverseWorldEditorPending) {
                    return;
                }
                if (window.saiverseWorldEditorInitialized) {
                    window.saiverseWorldEditorPending = false;
                    return;
                }
                const button = document.querySelector("#world_editor_refresh_btn button, #world_editor_refresh_btn");
                if (button) {
                    window.saiverseWorldEditorInitialized = true;
                    window.saiverseWorldEditorPending = false;
                    button.click();
                } else {
                    requestAnimationFrame(triggerWorldEditorLoad);
                }
            };
            const setActive = (label) => {
                const navItems = document.querySelectorAll("#saiverse-sidebar-nav .saiverse-nav-item");
                navItems.forEach((item) => {
                    const isActive = item.dataset.tabLabel === label;
                    item.classList.toggle("active", isActive);
                });
                Object.entries(sections).forEach(([name, selector]) => {
                    const el = document.querySelector(selector);
                    if (!el) {
                        return;
                    }
                    if (name === label) {
                        el.classList.remove("saiverse-hidden");
                    } else {
                        el.classList.add("saiverse-hidden");
                    }
                });
                window.saiverseActiveSection = label;
                if (label === "ワールドビュー") {
                    // 初回のみtriggerWorldviewLoadを呼ぶ
                    if (!window.saiverseWorldviewInitialized) {
                        window.saiverseAutoLoadEnabled = true;
                        window.saiverseWorldviewPending = true;
                        triggerWorldviewLoad();
                    }
                } else {
                    window.saiverseWorldviewPending = false;
                    window.saiverseAutoLoadEnabled = false;
                    if (label === "ワールドエディタ") {
                        if (!window.saiverseWorldEditorInitialized) {
                            window.saiverseWorldEditorPending = true;
                            triggerWorldEditorLoad();
                        }
                    } else {
                        window.saiverseWorldEditorPending = false;
                    }
                }
            };
            window.saiverseTriggerWorldviewLoad = triggerWorldviewLoad;
            window.saiverseTriggerWorldEditorLoad = triggerWorldEditorLoad;
            setActive(defaultLabel);

            const setupAttachmentControls = () => {
                const textarea = document.querySelector("#chat_message_textbox textarea");
                const fileInput = document.querySelector("#attachment_button input[type='file']");
                if (!textarea || !fileInput) {
                    return false;
                }
                if (textarea.dataset.dropHandlerAttached === "true") {
                    return true;
                }
                const highlightClass = "drop-target-active";
                const hasImage = (items) => {
                    if (!items) {
                        return false;
                    }
                    const list = Array.from(items);
                    if (!list.length) {
                        return false;
                    }
                    return list.some((item) => {
                        const type = item.type || "";
                        if (type.startsWith("image/")) {
                            return true;
                        }
                        const name = item.name || "";
                        return /\\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(name);
                    });
                };
                const addHighlight = () => textarea.classList.add(highlightClass);
                const removeHighlight = () => textarea.classList.remove(highlightClass);
                ["dragenter", "dragover"].forEach((eventName) => {
                    textarea.addEventListener(eventName, (event) => {
                        if (hasImage(event.dataTransfer?.items || event.dataTransfer?.files)) {
                            event.preventDefault();
                            event.dataTransfer.dropEffect = "copy";
                            addHighlight();
                        }
                    });
                });
                ["dragleave", "dragend"].forEach((eventName) => {
                    textarea.addEventListener(eventName, () => {
                        removeHighlight();
                    });
                });
                textarea.addEventListener("drop", (event) => {
                    const files = event.dataTransfer?.files;
                    if (!files || !files.length) {
                        removeHighlight();
                        return;
                    }
                    const imageFiles = Array.from(files).filter((file) => {
                        return file.type.startsWith("image/") || /\\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(file.name);
                    });
                    if (!imageFiles.length) {
                        removeHighlight();
                        return;
                    }
                    event.preventDefault();
                    let assigned = false;
                    try {
                        const transfer = new DataTransfer();
                        transfer.items.add(imageFiles[0]);
                        fileInput.files = transfer.files;
                        assigned = true;
                    } catch (error) {
                        try {
                            fileInput.files = files;
                            assigned = true;
                        } catch (_) {
                            assigned = false;
                        }
                    }
                    if (assigned) {
                        fileInput.dispatchEvent(new Event("change", { bubbles: true }));
                    }
                    removeHighlight();
                });
                textarea.dataset.dropHandlerAttached = "true";
                return true;
            };

            const setupSidebarOverlayDismiss = () => {
                const sidebar = document.querySelector(".sidebar.saiverse-sidebar");
                if (!sidebar) {
                    return false;
                }

                // すでにイベントが設定済みならスキップ
                if (sidebar.dataset.dismissHandlerAttached === "true") {
                    return true;
                }

                // body全体でクリックイベントをキャプチャ
                document.body.addEventListener("click", (e) => {
                    const isMobile = window.matchMedia("(max-width: 768px)").matches;
                    if (!isMobile) {
                        return; // PCでは何もしない
                    }

                    if (sidebar.classList.contains("open")) {
                        // サイドバー内部のクリックかどうかを判定
                        let target = e.target;
                        let isInsideSidebar = false;
                        while (target && target !== document.body) {
                            if (target === sidebar) {
                                isInsideSidebar = true;
                                break;
                            }
                            target = target.parentElement;
                        }

                        // サイドバー外をクリックした場合は閉じる
                        if (!isInsideSidebar) {
                            sidebar.classList.remove("open");
                            console.debug('[ui-js] sidebar closed by outside click');
                        }
                    }
                }, true); // キャプチャフェーズで処理

                sidebar.dataset.dismissHandlerAttached = "true";
                console.debug('[ui-js] sidebar dismiss handler attached');
                return true;
            };

            const attachNavHandlers = () => {
                const navItems = document.querySelectorAll("#saiverse-sidebar-nav .saiverse-nav-item");
                if (!navItems.length) {
                    return false;
                }
                navItems.forEach((item) => {
                    if (item.dataset.listenerAttached === "true") {
                        return;
                    }
                    item.dataset.listenerAttached = "true";
                    item.addEventListener("click", () => {
                        setActive(item.dataset.tabLabel);
                    });
                });
                return true;
            };

            const markSidebars = () => {
                let found = false;
                document.querySelectorAll(".sidebar").forEach((el) => {
                    if (!el.classList.contains("saiverse-sidebar")) {
                        el.classList.add("saiverse-sidebar");
                    }
                    const isMobile = window.matchMedia("(max-width: 768px)").matches;
                    const widthValue = isMobile ? "80vw" : "20vw";
                    const offsetValue = `calc(-1 * ${widthValue})`;
                    el.style.setProperty("width", widthValue, "important");
                    if (el.classList.contains("right")) {
                        el.style.removeProperty("left");
                        el.style.setProperty("right", offsetValue, "important");
                    } else {
                        el.style.removeProperty("right");
                        el.style.setProperty("left", offsetValue, "important");
                    }
                    // PCでは初期表示、モバイルでは非表示
                    if (!window.saiverseSidebarInitialized) {
                        if (!isMobile) {
                            el.classList.add("open");
                        }
                        window.saiverseSidebarInitialized = true;
                    }
                    found = true;
                });
                if (found) {
                    if (attachNavHandlers()) {
                        const current = window.saiverseActiveSection || defaultLabel;
                        setActive(current);
                    }
                    setupAttachmentControls();
                    setupSidebarOverlayDismiss();
                }
                return found;
            };

            if (!markSidebars()) {
                let attempts = 0;
                const watcher = setInterval(() => {
                    attempts += 1;
                    if (markSidebars() || attempts > 20) {
                        clearInterval(watcher);
                    }
                }, 250);
            }
            const ensureAttachmentSetup = () => {
                if (!setupAttachmentControls()) {
                    requestAnimationFrame(ensureAttachmentSetup);
                }
            };
            ensureAttachmentSetup();

            setInterval(() => {
                const button = document.getElementById("auto_refresh_log_btn");
                if (button) {
                    button.click();
                }
                markSidebars();
                setupAttachmentControls();
            }, 5000);
        }
        """
        demo.load(fn=get_current_building_history, inputs=None, outputs=[chatbot])
        demo.load(None, None, None, js=js_auto_refresh)


    return demo
