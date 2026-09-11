"""チュートリアルが新規ユーザーに配るモデル設定が、実在して役割をこなせる組み込みモデルを指す契約。

api/routes/tutorial.py の PROVIDER_PRESETS は、初回セットアップで全体設定 (.env) に
そのまま書き込まれる。組み込みモデルの定義 (builtin_data/models/) を削除・改名した
ときにプリセットを直し忘れると、新規ユーザーは初回起動の時点から、存在しない
モデルを指す設定を受け取る。存在していても、役割に要る能力が無いモデルを指すと、
その役割の処理だけが黙って止まる。

能力は同梱の定義ファイルを直接読んで判定する (ローダーを通すと、テストを走らせる
機械の user_data 上書きを見てしまう)。既定値は saiverse/model_configs.py に合わせる:
supports_structured_output は書かれていなければ True、supports_images は False。
"""
import json
from pathlib import Path

from api.routes import tutorial

BUILTIN_MODELS_DIR = Path(__file__).resolve().parent.parent / "builtin_data" / "models"


def _definition(key: str) -> dict:
    return json.loads((BUILTIN_MODELS_DIR / f"{key}.json").read_text(encoding="utf-8"))


def _answers_structured(key: str) -> bool:
    return bool(_definition(key).get("supports_structured_output", True))


def _reads_images(key: str) -> bool:
    return bool(_definition(key).get("supports_images"))


def test_every_preset_model_key_has_a_builtin_definition():
    missing = [
        f"{provider}.{role} = {key!r}"
        for provider, roles in tutorial.PROVIDER_PRESETS.items()
        for role, key in roles.items()
        if key is not None and not (BUILTIN_MODELS_DIR / f"{key}.json").is_file()
    ]

    assert missing == [], (
        "チュートリアルのプリセットが、builtin_data/models/ に定義の無いモデルを指しています:\n"
        + "\n".join(missing)
    )


def test_gemini_fallback_for_media_roles_has_a_builtin_definition():
    # プリセットが None の画像/音声/動画要約には、Gemini のキーがあればこの既定が配られる
    key = tutorial._GEMINI_IMAGE_SUMMARY_DEFAULT

    assert (BUILTIN_MODELS_DIR / f"{key}.json").is_file(), key
    assert _reads_images(key), key


def test_every_preset_can_answer_structured_requests():
    """判断のように決まった形の答えが要る処理を、どのプリセットでも引き受けられる。

    - sea/runtime.py の select_llm_client: 標準モデルが構造化出力に非対応なら軽量モデルへ
      回し、両方とも非対応なら例外で止まる。
    - sai_memory/curation_ops.py の編纂のページ分け: Memory Weave モデルに直接構造化出力を頼む。
    """
    problems = []
    for provider, roles in tutorial.PROVIDER_PRESETS.items():
        standard = roles.get("default_model")
        lightweight = roles.get("lightweight_model")
        weave = roles.get("memory_weave_model")
        if standard and lightweight and not (_answers_structured(standard) or _answers_structured(lightweight)):
            problems.append(f"{provider}: 標準 {standard!r} も軽量 {lightweight!r} も構造化出力に非対応")
        if weave and not _answers_structured(weave):
            problems.append(f"{provider}: Memory Weave {weave!r} が構造化出力に非対応")

    assert problems == [], "\n".join(problems)


def test_every_preset_image_summary_model_reads_images():
    unable = [
        f"{provider}: {roles['image_summary_model']!r}"
        for provider, roles in tutorial.PROVIDER_PRESETS.items()
        if roles.get("image_summary_model") and not _reads_images(roles["image_summary_model"])
    ]

    assert unable == [], "画像要約モデルに、画像を読めない定義が選ばれています:\n" + "\n".join(unable)
