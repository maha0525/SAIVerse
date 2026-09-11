"""チュートリアルが新規ユーザーに配るモデル設定が、実在する組み込みモデル定義を指す契約。

api/routes/tutorial.py の PROVIDER_PRESETS は、初回セットアップで全体設定 (.env) に
そのまま書き込まれる。組み込みモデルの定義 (builtin_data/models/) を削除・改名した
ときにプリセットを直し忘れると、新規ユーザーは初回起動の時点から、存在しない
モデルを指す設定を受け取る。
"""
from pathlib import Path

from api.routes import tutorial

BUILTIN_MODELS_DIR = Path(__file__).resolve().parent.parent / "builtin_data" / "models"


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
