"""Local image generation tool using ComfyUI API.

Connects to a running ComfyUI server, submits a workflow with customizable
prompts, monitors progress, and stores the resulting image as a SAIVerse item.
"""

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Optional, Tuple

from tools.core import ToolSchema, ToolResult
from tools.context import (
    get_active_persona_id,
    get_active_manager,
    get_active_playbook_name,
)
from saiverse.media_utils import store_image_bytes

logger = logging.getLogger(__name__)

COMFYUI_BASE_URL = "http://127.0.0.1:8188"
WORKFLOW_DIR = Path(__file__).parent / "comfyui_workflows"
PROGRESS_POLL_INTERVAL = 3  # seconds
PROGRESS_TIMEOUT = 300  # seconds (5 minutes)
STALL_TIMEOUT = 60  # seconds without progress before giving up


def _find_prompt_nodes(workflow: dict) -> Tuple[Optional[str], Optional[str]]:
    """Find positive and negative prompt node IDs in a workflow.

    Searches by _meta.title containing 'Positive' / 'Negative',
    or by the first two CLIPTextEncode nodes found.
    """
    positive_id = None
    negative_id = None
    clip_text_nodes = []

    for node_id, node in workflow.items():
        if node.get("class_type") != "CLIPTextEncode":
            continue
        clip_text_nodes.append(node_id)
        title = (node.get("_meta") or {}).get("title", "")
        if "Positive" in title or "positive" in title:
            positive_id = node_id
        elif "Negative" in title or "negative" in title:
            negative_id = node_id

    # Fallback: use ordering if titles don't match
    if positive_id is None and len(clip_text_nodes) >= 1:
        positive_id = clip_text_nodes[0]
    if negative_id is None and len(clip_text_nodes) >= 2:
        negative_id = clip_text_nodes[1]

    return positive_id, negative_id


def _queue_prompt(workflow: dict, client_id: str) -> str:
    """Submit a workflow to ComfyUI and return the prompt_id."""
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(
        f"{COMFYUI_BASE_URL}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
    except (urllib.error.URLError, OSError) as exc:
        raise ConnectionError(
            f"ComfyUIサーバー ({COMFYUI_BASE_URL}) に接続できませんでした: {exc}"
        ) from exc

    if "error" in result:
        raise RuntimeError(f"ComfyUI queue error: {result['error']}")
    if "node_errors" in result and result["node_errors"]:
        raise RuntimeError(
            f"ComfyUI node errors: {json.dumps(result['node_errors'], ensure_ascii=False)}"
        )

    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI returned no prompt_id: {result}")
    logger.info("ComfyUI prompt queued: prompt_id=%s", prompt_id)
    return prompt_id


def _wait_for_completion(prompt_id: str) -> dict:
    """Poll /history until the prompt completes or times out.

    Returns the history entry for the prompt, including outputs.
    """
    start = time.monotonic()
    last_progress_time = start

    while True:
        elapsed = time.monotonic() - start
        if elapsed > PROGRESS_TIMEOUT:
            raise TimeoutError(
                f"ComfyUI画像生成がタイムアウトしました ({PROGRESS_TIMEOUT}秒経過)"
            )
        stall_duration = time.monotonic() - last_progress_time
        if stall_duration > STALL_TIMEOUT:
            raise TimeoutError(
                f"ComfyUI画像生成が停滞しています ({STALL_TIMEOUT}秒間進捗なし)"
            )

        try:
            resp = urllib.request.urlopen(
                f"{COMFYUI_BASE_URL}/history/{prompt_id}", timeout=10
            )
            history = json.loads(resp.read())
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("Failed to poll ComfyUI history: %s", exc)
            time.sleep(PROGRESS_POLL_INTERVAL)
            continue

        if prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status", {})
            status_str = status.get("status_str", "")

            if status_str == "error":
                raise RuntimeError(
                    f"ComfyUI execution failed: {json.dumps(status, ensure_ascii=False)}"
                )

            if status.get("completed"):
                logger.info(
                    "ComfyUI prompt completed: prompt_id=%s (%.1fs)",
                    prompt_id,
                    time.monotonic() - start,
                )
                return entry

            # Progress detected (entry exists but not completed)
            last_progress_time = time.monotonic()

        time.sleep(PROGRESS_POLL_INTERVAL)


def _fetch_image(filename: str, subfolder: str = "", img_type: str = "output") -> bytes:
    """Fetch image data from ComfyUI /view endpoint."""
    params = urllib.parse.urlencode({
        "filename": filename,
        "subfolder": subfolder,
        "type": img_type,
    })
    url = f"{COMFYUI_BASE_URL}/view?{params}"
    try:
        resp = urllib.request.urlopen(url, timeout=30)
        return resp.read()
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"ComfyUI画像取得に失敗: {exc}") from exc


def generate_image_local(
    title: str,
    positive_prompt: str,
    negative_prompt: str = "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, sepia",
    workflow_file: str = "Anima.json",
    batch_count: int = 1,
) -> Tuple[str, ToolResult, Optional[str], Optional[dict]]:
    """Generate an image using a local ComfyUI server.

    Args:
        title: Image title (used as item name in SAIVerse).
        positive_prompt: Positive prompt text (natural language description).
        negative_prompt: Negative prompt text.
        workflow_file: Workflow JSON filename in comfyui_workflows directory.
        batch_count: Number of images to generate (each with a different seed).

    Returns:
        Tuple of (text, ToolResult, file_path, metadata).
    """
    # Load workflow template
    workflow_path = WORKFLOW_DIR / workflow_file
    if not workflow_path.exists():
        error = f"ワークフローファイルが見つかりません: {workflow_path}"
        logger.error(error)
        return error, ToolResult(None), None, None

    with open(workflow_path, "r", encoding="utf-8") as f:
        workflow_template = json.load(f)

    # Find and replace prompt nodes
    pos_node_id, neg_node_id = _find_prompt_nodes(workflow_template)
    if pos_node_id is None:
        error = "ワークフロー内にCLIPTextEncodeノードが見つかりません"
        logger.error(error)
        return error, ToolResult(None), None, None

    logger.info(
        "ComfyUI generate_image_local: title=%s, positive_node=%s, negative_node=%s, batch=%d",
        title, pos_node_id, neg_node_id, batch_count,
    )

    results = []
    client_id = str(uuid.uuid4())

    for i in range(batch_count):
        # Deep copy workflow for each batch
        workflow = json.loads(json.dumps(workflow_template))

        # Set prompts (replace placeholder or overwrite)
        workflow[pos_node_id]["inputs"]["text"] = positive_prompt
        if neg_node_id is not None:
            workflow[neg_node_id]["inputs"]["text"] = negative_prompt

        # Randomize seed for KSampler nodes
        for node_id, node in workflow.items():
            if node.get("class_type") == "KSampler":
                workflow[node_id]["inputs"]["seed"] = random.randint(0, 2**53)

        # Queue and wait
        try:
            prompt_id = _queue_prompt(workflow, client_id)
            entry = _wait_for_completion(prompt_id)
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            error = f"ComfyUI画像生成に失敗しました (batch {i + 1}/{batch_count}): {exc}"
            logger.error(error)
            return error, ToolResult(None), None, None

        # Extract output images
        outputs = entry.get("outputs", {})
        for node_id, node_output in outputs.items():
            images = node_output.get("images", [])
            for img_info in images:
                results.append(img_info)

    if not results:
        error = "ComfyUI: 生成された画像がありません"
        logger.error(error)
        return error, ToolResult(None), None, None

    # Fetch the first generated image (primary result)
    primary = results[0]
    image_data = _fetch_image(
        primary["filename"],
        primary.get("subfolder", ""),
        primary.get("type", "output"),
    )
    logger.info(
        "ComfyUI image fetched: filename=%s, size=%d bytes",
        primary["filename"], len(image_data),
    )

    # Store in SAIVerse
    metadata_entry, stored_path = store_image_bytes(
        image_data, "image/png", source="tool:generate_image_local:comfyui"
    )
    snippet = f"![{title}]({stored_path.as_posix()})"
    metadata = {"media": [metadata_entry]}

    # Create picture item
    item_text = ""
    try:
        persona_id = get_active_persona_id()
        manager = get_active_manager()

        if persona_id and manager:
            playbook_name = get_active_playbook_name()
            source_context = json.dumps(
                {"playbook": playbook_name, "tool": "generate_image_local"}
            )
            item_name = title if title else f"生成画像_{stored_path.stem}"
            item_id = manager.create_picture_item(
                persona_id=persona_id,
                name=item_name,
                description=positive_prompt,
                file_path=str(stored_path),
                source_context=source_context,
            )
            item_uri = f"saiverse://item/{item_id}/image"
            item_text = (
                f"\n\n画像をアイテムとして登録しました"
                f"（アイテムID: {item_id}、URI: {item_uri}）。"
            )
    except Exception as exc:
        logger.warning("Failed to create picture item: %s", exc)

    text = (
        f"ローカル画像生成が完了しました。\n\n"
        f"タイトル: {title}\n"
        f"ワークフロー: {workflow_file}\n"
        f"プロンプト:\n{positive_prompt}"
        f"{item_text}"
    )

    return text, ToolResult(snippet), stored_path.as_posix(), metadata


def schema() -> ToolSchema:
    return ToolSchema(
        name="generate_image_local",
        description=(
            "Generate an image using a local ComfyUI server. "
            "Supports customizable workflows with positive/negative prompts. "
            "The Anima model supports natural language prompts (not SD-style tag syntax). "
            "Write prompts as detailed English descriptions."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Image title (used as the SAIVerse item name)",
                },
                "positive_prompt": {
                    "type": "string",
                    "description": (
                        "Positive prompt: a detailed natural language description "
                        "of the desired image in English. NOT SD-style comma-separated tags."
                    ),
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "Negative prompt (things to avoid in the image)",
                    "default": "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, sepia",
                },
                "workflow_file": {
                    "type": "string",
                    "description": "Workflow JSON filename in comfyui_workflows directory",
                    "default": "Anima.json",
                },
                "batch_count": {
                    "type": "integer",
                    "description": "Number of images to generate",
                    "default": 1,
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["title", "positive_prompt"],
        },
        result_type="string",
    )
