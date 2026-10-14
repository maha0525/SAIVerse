"""Embedder (EmbeddingGemma 2, ONNX via onnxruntime) with the model mocked out.

The onnxruntime session is replaced by a fake that records its feeds; the
tokenizer is a real ``tokenizers`` WordLevel tokenizer written to a temp
tokenizer.json, so truncation and special-token handling are the library's own.
Real-model behaviour is checked separately (smoke test against the bench).
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from tokenizers import Tokenizer, models, pre_tokenizers, processors

from sai_memory import config as sm_config
from sai_memory.memory import recall

PAD, EOS, BOS = 0, 1, 2


class FakeSession:
    instances: list["FakeSession"] = []

    def __init__(self, path, sess_options=None, providers=None):
        self.path = path
        self.sess_options = sess_options
        self.providers = providers
        self.calls: list[dict] = []
        FakeSession.instances.append(self)

    def get_inputs(self):
        names = ("input_ids", "attention_mask", "image_features", "video_features", "audio_features")
        return [SimpleNamespace(name=n) for n in names]

    def get_outputs(self):
        return [SimpleNamespace(name="last_hidden_state"), SimpleNamespace(name="sentence_embedding")]

    def run(self, output_names, feeds):
        assert output_names == ["sentence_embedding"]
        self.calls.append(feeds)
        n = feeds["input_ids"].shape[0]
        # Deliberately not unit length; element 0 encodes the unpadded length.
        out = np.full((n, 768), 3.0, dtype=np.float32)
        out[:, 0] = feeds["attention_mask"].sum(axis=1) * 10.0
        return [out]


def _write_model_dir(directory):
    tok = Tokenizer(models.WordLevel({"<pad>": PAD, "<eos>": EOS, "<bos>": BOS, "<unk>": 3, "w": 4}, unk_token="<unk>"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.post_processor = processors.TemplateProcessing(
        single="<bos> $A <eos>", special_tokens=[("<bos>", BOS), ("<eos>", EOS)]
    )
    (directory / "onnx").mkdir(parents=True, exist_ok=True)
    tok.save(str(directory / "tokenizer.json"))
    for rel in sm_config.EMBED_MODEL_FILES:
        path = directory / rel
        if not path.exists():
            path.write_bytes(b"")
    return directory


@pytest.fixture
def fake_runtime(monkeypatch):
    import onnxruntime

    FakeSession.instances = []
    monkeypatch.setattr(onnxruntime, "InferenceSession", FakeSession)
    monkeypatch.setattr(recall, "_EMBEDDING_MODEL_CACHE", {})
    return FakeSession


@pytest.fixture
def model_dir(tmp_path):
    return _write_model_dir(tmp_path / "embeddinggemma-2-q4")


def _token_count(embedder, text):
    return len(embedder._encoder.tokenizer.encode(text).ids)


def test_identity_dimension_and_cpu_only(fake_runtime, model_dir):
    emb = recall.Embedder(local_model_path=str(model_dir))
    assert emb.model_name == "embeddinggemma-2-q4@daa72c51"
    assert emb.embedding_size == 768
    (session,) = fake_runtime.instances
    assert session.providers == ["CPUExecutionProvider"]
    assert session.path.endswith("model_q4.onnx")


def test_query_and_document_prompts(fake_runtime, model_dir):
    emb = recall.Embedder(local_model_path=str(model_dir))
    seen: list[list[str]] = []
    real_encode = emb._encoder.encode

    def spy(texts):
        seen.append(list(texts))
        return real_encode(texts)

    emb._encoder.encode = spy
    emb.embed(["a b"], is_query=True)
    emb.embed(["c d"], is_query=False)
    emb.embed(["e f"])
    assert seen == [
        ["task: search result | query: a b"],
        ["title: none | text: c d"],
        ["title: none | text: e f"],
    ]


def test_output_is_renormalised_python_floats(fake_runtime, model_dir):
    emb = recall.Embedder(local_model_path=str(model_dir))
    vectors = emb.embed(["w w", "w"])
    assert len(vectors) == 2
    for v in vectors:
        assert len(v) == 768
        assert all(type(x) is float for x in v)
        assert np.linalg.norm(v) == pytest.approx(1.0, abs=1e-6)
    assert emb.embed([]) == []


def test_input_is_truncated_to_512_tokens(fake_runtime, model_dir):
    emb = recall.Embedder(local_model_path=str(model_dir))
    emb.embed([" ".join(["w"] * 2000)])
    (feeds,) = fake_runtime.instances[0].calls
    ids = feeds["input_ids"]
    assert ids.shape == (1, 512)
    assert ids[0, 0] == BOS
    assert ids[0, -1] == EOS  # truncation keeps the special tokens


def test_batching_padding_media_inputs_and_order(fake_runtime, model_dir):
    emb = recall.Embedder(local_model_path=str(model_dir))
    texts = [" ".join(["w"] * ((i * 7) % 23 + 1)) for i in range(40)]
    vectors = np.array(emb.embed(texts))

    calls = fake_runtime.instances[0].calls
    assert [c["input_ids"].shape[0] for c in calls] == [32, 8]
    for feeds in calls:
        ids, mask = feeds["input_ids"], feeds["attention_mask"]
        assert ids.dtype == np.int64 and mask.dtype == np.int64
        # right padding with pad id 0
        assert np.all(ids[mask == 0] == PAD)
        lengths = mask.sum(axis=1)
        assert np.all(mask[np.arange(len(lengths)), lengths - 1] == 1)
        # longest first
        assert list(lengths) == sorted(lengths, reverse=True)
        for name in ("image_features", "video_features", "audio_features"):
            assert feeds[name].shape == (0, 512)
            assert feeds[name].dtype == np.float32

    # results come back in input order: element 0 / element 1 = length * 10 / 3
    prompt = recall.DOCUMENT_PROMPT
    for text, v in zip(texts, vectors):
        expected = _token_count(emb, prompt + text) * 10.0 / 3.0
        assert v[0] / v[1] == pytest.approx(expected, rel=1e-5)


def test_long_inputs_are_split_by_the_token_budget(fake_runtime, model_dir):
    # A fixed row count let 32 rows of 512 tokens peak at 8.5GB; each call is
    # bounded by padded tokens instead (rows x longest row).
    emb = recall.Embedder(local_model_path=str(model_dir))
    long_texts = [" ".join(["w"] * 2000) for _ in range(10)]  # truncated to 512
    short_texts = [" ".join(["w"] * 3) for _ in range(5)]
    vectors = emb.embed(long_texts + short_texts)
    assert len(vectors) == 15

    calls = fake_runtime.instances[0].calls
    rows = [c["input_ids"].shape[0] for c in calls]
    # the third call starts with a long row, so it takes 4 rows padded to 512
    assert rows == [4, 4, 4, 3]
    for feeds in calls:
        assert feeds["input_ids"].size <= recall._MAX_BATCH_TOKENS


def test_cpu_memory_arena_is_disabled(fake_runtime, model_dir):
    # With the arena on, the largest batch's buffers stayed resident for the
    # life of the process.
    recall.Embedder(local_model_path=str(model_dir))
    (session,) = fake_runtime.instances
    assert session.sess_options.enable_cpu_mem_arena is False
    assert session.sess_options.enable_mem_pattern is False


def test_model_is_shared_within_the_process(fake_runtime, model_dir):
    a = recall.Embedder(local_model_path=str(model_dir))
    b = recall.Embedder(local_model_path=str(model_dir))
    assert a._encoder is b._encoder
    assert len(fake_runtime.instances) == 1


def test_explicit_dir_with_missing_files_raises(fake_runtime, tmp_path):
    with pytest.raises(FileNotFoundError, match="model_q4.onnx"):
        recall.Embedder(local_model_path=str(tmp_path / "empty"))


def test_default_dir_downloads_pinned_files(fake_runtime, tmp_path, monkeypatch):
    import huggingface_hub

    target = tmp_path / "sbert" / "embeddinggemma-2-q4"
    monkeypatch.setattr(recall, "default_embed_model_dir", lambda: target)
    calls = []

    def fake_snapshot_download(repo_id, **kwargs):
        calls.append((repo_id, kwargs))
        _write_model_dir(target)
        return str(target)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)
    emb = recall.Embedder()
    assert emb.embedding_size == 768
    assert calls == [
        (
            "onnx-community/embeddinggemma-2-ONNX",
            {
                "revision": "daa72c51243991dfcaf9f9137d2c573d8f7790c0",
                "local_dir": str(target.resolve()),
                "allow_patterns": list(sm_config.EMBED_MODEL_FILES),
            },
        )
    ]
    # Already present: a second Embedder in a fresh process-cache does not download.
    monkeypatch.setattr(recall, "_EMBEDDING_MODEL_CACHE", {})
    recall.Embedder()
    assert len(calls) == 1


def test_settings_are_fixed_to_the_new_model(monkeypatch, tmp_path):
    for name in ("SAIMEMORY_EMBED_MODEL", "SAIMEMORY_EMBED_MODEL_PATH", "SAIMEMORY_EMBED_MODEL_DIM"):
        monkeypatch.setenv(name, "intfloat/multilingual-e5-small")
    target = _write_model_dir(tmp_path / "embeddinggemma-2-q4")
    monkeypatch.setattr(sm_config, "default_embed_model_dir", lambda: target)
    settings = sm_config.load_settings()
    assert settings.embed_model == "embeddinggemma-2-q4@daa72c51"
    assert settings.embed_model_path == str(target)

    (target / "tokenizer.json").unlink()
    assert sm_config.load_settings().embed_model_path is None
