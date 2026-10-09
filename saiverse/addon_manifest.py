"""addon.json v2 manifest スキーマ (setup / uninstall step の宣言用)。

設計は `docs/intent/addon_catalog_management.md` を参照。

このモジュールは installer / カタログ UI 用の追加スキーマだけを定義する。
既存の addon_loader.py は addon.json を生の dict として読むため後方互換は壊さない。

manifest_version:
    1 (省略時): 既存の addon.json。setup / uninstall フィールドなし
    2: setup.steps / uninstall.steps / setup_version / data_subdirs を扱う

step.type の allowlist:
    - pip_install:       requirements ファイルを `python -m pip install -r` する
    - platform_script:   addon ディレクトリ内の OS 別スクリプト実行
    - python_script:     addon ディレクトリ内の .py を python で実行
    - remove_dir:        addon ディレクトリ配下 or 永続データ配下のパスを削除
    - git_clone:         サブリポジトリを clone (commit SHA pin 必須)
    - download_file:     URL から DL (SHA256 検証必須)

導入時の質問 (setup.options) と step の条件 (2026-10-05):
    - setup.options:  利用者に選ばせる質問の一覧。答えは選択肢の id だけで、
                      自由入力は無い (答えが step の引数に入り込む経路を作らない)
    - step.when:      {質問 id: 選択肢 id}。全部が答えに含まれるときだけ実行
    - step.os:        windows / linux / macos の一覧。実行中の OS が無ければ飛ばす
    - step.env:       pip_install / python_script / platform_script をアドオン専用の
                      Python 環境 (~/.saiverse/addon_install/<id>/envs/<env>/) で起動する

セキュリティ制約 (Pydantic validator で強制):
    - パスは addon ディレクトリ相対、'..' 不可、絶対パス不可
    - download_file の dest は永続データディレクトリ内固定
    - git_clone の commit は省略不可 (HEAD 追従禁止)
    - env の名前・質問と選択肢の id は [A-Za-z0-9_-] だけ
    - when が参照する質問と選択肢は setup.options に実在すること
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_SAFE_RELPATH_RE = re.compile(r"^[A-Za-z0-9_\-./]+$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# env の名前、質問と選択肢の id。env の名前はそのままフォルダ名になるので、
# パス区切りも '.' も許さない ('..' はこれで自動的に弾かれる)。
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# step.os の語彙。registry の requires.os と同じ。
StepOs = Literal["windows", "linux", "macos"]


def _validate_safe_name(value: str, field_name: str) -> str:
    if not _SAFE_NAME_RE.match(value or ""):
        raise ValueError(
            f"{field_name}: only [A-Za-z0-9_-] is allowed, got {value!r}"
        )
    return value


def _validate_safe_relpath(value: str, field_name: str) -> str:
    """addon ディレクトリ相対の安全なパス文字列であることを検証。

    禁止: 絶対パス、'..' を含むパス、許可文字以外。
    """
    if not value:
        raise ValueError(f"{field_name}: path must not be empty")
    p = PurePosixPath(value.replace("\\", "/"))
    if p.is_absolute():
        raise ValueError(f"{field_name}: absolute paths are not allowed ({value!r})")
    if ".." in p.parts:
        raise ValueError(f"{field_name}: '..' is not allowed in paths ({value!r})")
    if not _SAFE_RELPATH_RE.match(value.replace("\\", "/")):
        raise ValueError(f"{field_name}: path contains disallowed characters ({value!r})")
    return value


class _StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="UI 進捗表示用のステップ名")
    skip_if_exists: Optional[str] = Field(
        None,
        description=(
            "このパス (addon ディレクトリ相対) が既存ならステップをスキップ。"
            "voice-tts の external/GPT-SoVITS/ 再 DL 回避等に使う。"
        ),
    )
    when: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "{質問 id: 選択肢 id}。全部の質問の答えにその選択肢が含まれるときだけ"
            "実行する。空なら全員に実行する。"
        ),
    )
    os: Optional[List[StepOs]] = Field(
        None,
        description="この OS でだけ実行する (windows / linux / macos)。省略時は全 OS。",
    )

    @field_validator("skip_if_exists")
    @classmethod
    def _check_skip_path(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_safe_relpath(v, "skip_if_exists")

    @field_validator("when")
    @classmethod
    def _check_when(cls, v: Dict[str, str]) -> Dict[str, str]:
        for qid, cid in v.items():
            _validate_safe_name(qid, "when (question id)")
            _validate_safe_name(cid, "when (choice id)")
        return v

    @field_validator("os")
    @classmethod
    def _check_os(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is not None and not v:
            raise ValueError("os: must list at least one OS when given")
        return v


class _EnvStepMixin(BaseModel):
    """アドオン専用の Python 環境で起動できる step に付ける ``env``。"""

    model_config = ConfigDict(extra="forbid")

    env: Optional[str] = Field(
        None,
        description=(
            "この step を ~/.saiverse/addon_install/<id>/envs/<env>/ の Python で起動する。"
            "pip_install には本体の requirements.lock を constraints として渡さない。"
        ),
    )

    @field_validator("env")
    @classmethod
    def _check_env(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_safe_name(v, "env")


class PipInstallStep(_EnvStepMixin, _StepBase):
    type: Literal["pip_install"]
    requirements: str = Field(
        ..., description="addon ディレクトリ相対の requirements ファイルパス"
    )

    @field_validator("requirements")
    @classmethod
    def _check_requirements(cls, v: str) -> str:
        return _validate_safe_relpath(v, "requirements")


class PlatformScriptStep(_EnvStepMixin, _StepBase):
    type: Literal["platform_script"]
    windows: Optional[str] = Field(None, description="Windows で実行するスクリプト (相対パス)")
    unix: Optional[str] = Field(None, description="macOS/Linux で実行するスクリプト (相対パス)")

    @field_validator("windows", "unix")
    @classmethod
    def _check_script_path(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_safe_relpath(v, "platform_script")

    def model_post_init(self, _ctx) -> None:  # type: ignore[override]
        if not self.windows and not self.unix:
            raise ValueError(
                "platform_script: at least one of 'windows' / 'unix' must be set"
            )


class PythonScriptStep(_EnvStepMixin, _StepBase):
    type: Literal["python_script"]
    script: str = Field(..., description="addon ディレクトリ相対の .py ファイルパス")
    args: List[str] = Field(default_factory=list, description="スクリプトに渡す引数")

    @field_validator("script")
    @classmethod
    def _check_script(cls, v: str) -> str:
        v = _validate_safe_relpath(v, "script")
        if not v.endswith(".py"):
            raise ValueError(f"python_script.script must end with .py ({v!r})")
        return v


class RemoveDirStep(_StepBase):
    type: Literal["remove_dir"]
    path: str = Field(..., description="addon ディレクトリ相対の削除対象パス")
    target: Literal["addon", "data"] = Field(
        "addon",
        description=(
            "'addon': expansion_data/<id>/<path> を削除。"
            "'data': ~/.saiverse/user_data/addon_data/<id>/<path> を削除。"
        ),
    )

    @field_validator("path")
    @classmethod
    def _check_path(cls, v: str) -> str:
        return _validate_safe_relpath(v, "remove_dir.path")


class GitCloneStep(_StepBase):
    type: Literal["git_clone"]
    url: str = Field(..., description="clone する Git リポジトリ URL (https 推奨)")
    commit: str = Field(..., description="checkout する full commit SHA (40 hex 必須、HEAD 追従不可)")
    dest: str = Field(..., description="addon ディレクトリ相対の clone 先パス")

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError(f"git_clone.url must use HTTPS ({v!r})")
        return v

    @field_validator("commit")
    @classmethod
    def _check_commit(cls, v: str) -> str:
        if not _GIT_SHA_RE.match(v):
            raise ValueError(
                f"git_clone.commit must be a full lowercase 40-character SHA, got {v!r}. "
                "Branch / tag names are not allowed (HEAD 追従禁止)."
            )
        return v

    @field_validator("dest")
    @classmethod
    def _check_dest(cls, v: str) -> str:
        return _validate_safe_relpath(v, "git_clone.dest")


class DownloadFileStep(_StepBase):
    type: Literal["download_file"]
    url: str = Field(..., description="DL する URL (https 推奨)")
    sha256: str = Field(..., description="期待される SHA256 (64 hex)")
    dest_data: str = Field(
        ...,
        description=(
            "永続データディレクトリ ~/.saiverse/user_data/addon_data/<id>/ 内の "
            "相対パス。addon ディレクトリへの書き込みは禁止 (DL 物は永続データ扱い)。"
        ),
    )

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError(f"download_file.url must use HTTPS ({v!r})")
        return v

    @field_validator("sha256")
    @classmethod
    def _check_sha256(cls, v: str) -> str:
        if not _SHA256_RE.match(v):
            raise ValueError(f"download_file.sha256 must be 64 hex chars, got {v!r}")
        return v.lower()

    @field_validator("dest_data")
    @classmethod
    def _check_dest(cls, v: str) -> str:
        return _validate_safe_relpath(v, "download_file.dest_data")


SetupStep = Annotated[
    Union[
        PipInstallStep,
        PlatformScriptStep,
        PythonScriptStep,
        RemoveDirStep,
        GitCloneStep,
        DownloadFileStep,
    ],
    Field(discriminator="type"),
]


class SetupChoice(BaseModel):
    """導入時の質問の選択肢。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: Union[str, Dict[str, str]] = Field(..., description="表示文 (言語ごとの辞書も可)")
    default: bool = Field(False, description="既定で選ばれている選択肢か")

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        return _validate_safe_name(v, "choice id")


class SetupOption(BaseModel):
    """導入時に利用者へ出す質問。選択肢から選ぶ形だけ (自由入力は無い)。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    question: Union[str, Dict[str, str]] = Field(..., description="質問文 (言語ごとの辞書も可)")
    multiple: bool = Field(False, description="True で複数選べる。False は一つだけ")
    choices: List[SetupChoice] = Field(..., min_length=1)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        return _validate_safe_name(v, "question id")

    @model_validator(mode="after")
    def _check_choices(self) -> "SetupOption":
        ids = [c.id for c in self.choices]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise ValueError(f"question {self.id!r}: duplicate choice id(s) {dup}")
        if not self.multiple and sum(1 for c in self.choices if c.default) > 1:
            raise ValueError(
                f"question {self.id!r}: a single-choice question can have at most one default"
            )
        return self

    def choice_ids(self) -> List[str]:
        return [c.id for c in self.choices]

    def default_answer(self) -> List[str]:
        """既定の答え。一つだけ選ぶ質問で既定が無ければ、最初の選択肢。"""
        defaults = [c.id for c in self.choices if c.default]
        if self.multiple:
            return defaults
        return defaults[:1] or [self.choices[0].id]


def _check_when_refs(steps: List["SetupStep"], options: List[SetupOption], where: str) -> None:
    by_id = {o.id: o for o in options}
    for step in steps:
        for qid, cid in step.when.items():
            opt = by_id.get(qid)
            if opt is None:
                raise ValueError(
                    f"{where} step {step.name!r}: when refers to unknown question {qid!r}"
                )
            if cid not in opt.choice_ids():
                raise ValueError(
                    f"{where} step {step.name!r}: when refers to unknown choice "
                    f"{cid!r} of question {qid!r}"
                )


class SetupSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    options: List[SetupOption] = Field(
        default_factory=list, description="導入時に利用者へ出す質問の一覧"
    )
    steps: List[SetupStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_options(self) -> "SetupSection":
        ids = [o.id for o in self.options]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise ValueError(f"setup.options: duplicate question id(s) {dup}")
        _check_when_refs(self.steps, self.options, "setup")
        return self

    def get_option(self, question_id: str) -> Optional[SetupOption]:
        for o in self.options:
            if o.id == question_id:
                return o
        return None


class UninstallSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: List[SetupStep] = Field(
        default_factory=list,
        description="アンインストール時に実行するステップ (主に remove_dir)",
    )


class AddonManifest(BaseModel):
    """addon.json の installer/カタログ関連部分のみを表現するモデル。

    既存の addon.json には params_schema / ui_extensions / server_hooks 等の
    フィールドも入っているが、それらはこのモデルでは扱わない (extra='ignore')。
    addon_loader.py 側が引き続き raw dict として読む。
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., description="アドオン ID (expansion_data/ 下のディレクトリ名と一致)")
    display_name: Union[str, dict[str, str]] = Field(..., description="UI 表示名")
    display_name_en: Optional[str] = Field(None, description="UI 表示名 (英語)")
    description: Union[str, dict[str, str]] = Field("", description="UI 表示用の説明")
    description_en: Optional[str] = Field(None, description="UI 表示用の説明 (英語)")
    version: str = Field(..., description="アドオンのセマンティックバージョン")

    manifest_version: int = Field(
        1,
        description=(
            "manifest スキーマバージョン。setup / uninstall を使う場合は 2 必須。"
            "省略時は 1 (legacy) として扱う。"
        ),
    )
    setup_version: int = Field(
        1,
        description=(
            "setup の再実行が必要な変更があったらインクリメントする。"
            "アップデート時にこの値が増えていれば installer は setup.steps を再実行。"
        ),
    )

    setup: Optional[SetupSection] = None
    uninstall: Optional[UninstallSection] = None

    data_subdirs: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "永続データディレクトリ配下のサブディレクトリラベル (任意)。"
            "アンインストール確認 UI で 'inputs: 参照音声' 等を見せる用途のみ。"
        ),
    )

    def get_display_name(self, lang: str = "ja") -> str:
        from saiverse.i18n_utils import resolve_i18n_text
        return resolve_i18n_text(
            self.display_name,
            target_lang=lang,
            alt_en=self.display_name_en,
            default=self.name,
        )

    def get_description(self, lang: str = "ja") -> str:
        from saiverse.i18n_utils import resolve_i18n_text
        return resolve_i18n_text(
            self.description,
            target_lang=lang,
            alt_en=self.description_en,
            default="",
        )

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not re.match(r"^[a-z][a-z0-9\-_]*$", v):
            raise ValueError(
                f"addon name must be lowercase, start with a letter, "
                f"and contain only [a-z0-9_-]; got {v!r}"
            )
        return v

    @property
    def setup_options(self) -> List[SetupOption]:
        return list(self.setup.options) if self.setup else []

    @property
    def setup_steps(self) -> List["SetupStep"]:
        return list(self.setup.steps) if self.setup else []

    @model_validator(mode="after")
    def _check_uninstall_when(self) -> "AddonManifest":
        # uninstall の when も setup.options の質問を指す (答えは導入時のもの)
        if self.uninstall is not None:
            _check_when_refs(self.uninstall.steps, self.setup_options, "uninstall")
        return self

    def model_post_init(self, _ctx) -> None:  # type: ignore[override]
        if self.manifest_version >= 2:
            if self.setup is None and self.uninstall is None:
                # v2 でも setup/uninstall が両方空なのは許容 (Elyth 等 API キーのみ)
                pass
        else:
            if self.setup is not None or self.uninstall is not None:
                raise ValueError(
                    "manifest_version < 2 では setup / uninstall フィールドは使えません"
                )


def load_manifest(manifest_dict: dict) -> AddonManifest:
    """生の dict から AddonManifest を生成 (Pydantic validation)。

    validation エラーは pydantic.ValidationError として raise されるので
    呼び出し側で適切に handle すること。
    """
    return AddonManifest.model_validate(manifest_dict)


__all__ = [
    "AddonManifest",
    "SetupChoice",
    "SetupOption",
    "SetupSection",
    "StepOs",
    "UninstallSection",
    "SetupStep",
    "PipInstallStep",
    "PlatformScriptStep",
    "PythonScriptStep",
    "RemoveDirStep",
    "GitCloneStep",
    "DownloadFileStep",
    "load_manifest",
]
