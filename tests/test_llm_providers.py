"""Choosing where the thinking happens: Gemini, Groq, OpenAI, Ollama, LM Studio.

Two things must hold. Gemini stays the default and behaves exactly as before,
and the alternative providers must fail with a message a person can act on
rather than a stack trace — a local model that is simply not running is the
most likely error in the whole feature.

    .venv\\Scripts\\python.exe -m pytest tests/test_llm_providers.py -q
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import llm  # noqa: E402

MOMENTS_SCHEMA = {"type": "array", "items": {"type": "object"}}
OBJECT_SCHEMA = {"type": "object", "properties": {"hook_title": {"type": "string"}}}


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
	"""Config lives in the working directory — never touch the real one."""
	monkeypatch.chdir(tmp_path)
	yield


def configure(**cfg):
	from core import utils

	utils.save_config(cfg)


def fake_reply(content: str):
	payload = {"choices": [{"message": {"content": content}}]}
	return io.BytesIO(json.dumps(payload).encode())


def capture_request(monkeypatch, content: str = '{"ok": true}'):
	"""Answer the HTTP call and hand back what was sent."""
	sent = {}

	class Resp(io.BytesIO):
		def __enter__(self):
			return self

		def __exit__(self, *a):
			return False

	def fake_open(req, timeout=None):
		sent["url"] = req.full_url
		sent["headers"] = dict(req.headers)
		sent["body"] = json.loads(req.data)
		return Resp(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

	monkeypatch.setattr(llm.urllib.request, "urlopen", fake_open)
	return sent


# ── the default is untouched ─────────────────────────────────────────────────


def test_gemini_is_the_default_with_no_config():
	assert llm.is_gemini() is True
	assert llm.settings()["provider"] == "gemini"


def test_an_unknown_provider_falls_back_to_gemini():
	configure(ai_provider="something-invented")
	assert llm.is_gemini() is True


def test_generate_json_refuses_to_run_for_gemini():
	"""Gemini goes through the google-genai client, not this path. Silently
	handling it here would mean two code paths claiming to be the same one."""
	with pytest.raises(llm.LLMError):
		llm.generate_json("prompt", OBJECT_SCHEMA)


# ── settings resolution ──────────────────────────────────────────────────────


def test_each_provider_has_a_working_default_model_and_url():
	for name, preset in llm.PROVIDERS.items():
		if name == "gemini":
			continue
		assert preset["base_url"].startswith("http")
		assert preset["model"]


def test_a_custom_model_and_url_override_the_preset():
	configure(ai_provider="ollama", ai_model="qwen2.5:14b", ai_base_url="http://box:11434/v1/")
	cfg = llm.settings()
	assert cfg["model"] == "qwen2.5:14b"
	assert cfg["base_url"] == "http://box:11434/v1"   # trailing slash trimmed


def test_local_providers_do_not_need_a_key():
	configure(ai_provider="ollama")
	assert llm.settings()["needs_key"] is False


# ── the request we send ──────────────────────────────────────────────────────


def test_the_request_goes_to_the_right_endpoint_with_the_key(monkeypatch):
	configure(ai_provider="groq", ai_key="gsk_secret")
	sent = capture_request(monkeypatch)
	llm.generate_json("find the moments", OBJECT_SCHEMA)

	assert sent["url"] == "https://api.groq.com/openai/v1/chat/completions"
	assert sent["headers"]["Authorization"] == "Bearer gsk_secret"
	assert sent["body"]["model"] == llm.PROVIDERS["groq"]["model"]
	assert sent["body"]["response_format"] == {"type": "json_object"}


def test_the_schema_is_described_to_the_model(monkeypatch):
	"""These providers cannot enforce the schema, so it has to be in the prompt
	or the reply comes back in whatever shape the model felt like."""
	configure(ai_provider="ollama")
	sent = capture_request(monkeypatch)
	llm.generate_json("the transcript", OBJECT_SCHEMA)

	system = sent["body"]["messages"][0]["content"]
	assert "hook_title" in system and "JSON" in system
	assert sent["body"]["messages"][1]["content"] == "the transcript"


def test_a_missing_key_is_caught_before_any_request(monkeypatch):
	configure(ai_provider="openai")
	monkeypatch.setattr(
		llm.urllib.request, "urlopen", lambda *a, **k: pytest.fail("should not call out")
	)
	with pytest.raises(llm.LLMError) as exc:
		llm.generate_json("x", OBJECT_SCHEMA)
	assert "Settings" in str(exc.value)


# ── reading the reply ────────────────────────────────────────────────────────


def test_plain_json_is_parsed(monkeypatch):
	configure(ai_provider="ollama")
	capture_request(monkeypatch, '{"hook_title": "the bit about pricing"}')
	assert llm.generate_json("x", OBJECT_SCHEMA)["hook_title"] == "the bit about pricing"


def test_json_wrapped_in_a_markdown_fence_is_parsed(monkeypatch):
	"""Local models do this constantly, whatever the prompt says."""
	configure(ai_provider="ollama")
	capture_request(monkeypatch, 'Sure!\n```json\n{"hook_title": "x"}\n```\n')
	assert llm.generate_json("x", OBJECT_SCHEMA)["hook_title"] == "x"


def test_json_buried_in_prose_is_recovered(monkeypatch):
	configure(ai_provider="ollama")
	capture_request(monkeypatch, 'Here you go: {"hook_title": "x"} — hope that helps!')
	assert llm.generate_json("x", OBJECT_SCHEMA)["hook_title"] == "x"


def test_a_list_wrapped_in_an_object_is_unwrapped(monkeypatch):
	"""JSON mode forces an object on some providers, but the moments schema is a
	list. Without this the caller gets a dict where it expects clips."""
	configure(ai_provider="groq", ai_key="k")
	capture_request(monkeypatch, '{"moments": [{"start": 1}, {"start": 2}]}')
	result = llm.generate_json("x", MOMENTS_SCHEMA)
	assert isinstance(result, list) and len(result) == 2


def test_a_reply_with_no_json_at_all_says_so(monkeypatch):
	configure(ai_provider="ollama")
	capture_request(monkeypatch, "I am afraid I cannot do that.")
	with pytest.raises(llm.LLMError) as exc:
		llm.generate_json("x", OBJECT_SCHEMA)
	assert "JSON" in str(exc.value)


# ── failures a person has to understand ──────────────────────────────────────


def raise_http(code: int, body: dict | None = None):
	def fake(*a, **k):
		raise urllib.error.HTTPError(
			"http://x", code, "err", {}, io.BytesIO(json.dumps(body or {}).encode())
		)

	return fake


def test_a_rejected_key_says_to_check_settings(monkeypatch):
	configure(ai_provider="groq", ai_key="wrong")
	monkeypatch.setattr(llm.urllib.request, "urlopen", raise_http(401))
	with pytest.raises(llm.LLMError) as exc:
		llm.generate_json("x", OBJECT_SCHEMA)
	assert "key" in str(exc.value).lower() and "Settings" in str(exc.value)


def test_a_wrong_model_name_names_the_model(monkeypatch):
	configure(ai_provider="ollama", ai_model="llama-not-installed")
	monkeypatch.setattr(llm.urllib.request, "urlopen", raise_http(404))
	with pytest.raises(llm.LLMError) as exc:
		llm.generate_json("x", OBJECT_SCHEMA)
	assert "llama-not-installed" in str(exc.value)


def test_a_local_model_that_is_not_running_says_exactly_that(monkeypatch):
	"""The most likely failure in this whole feature. 'Connection refused' would
	tell the customer nothing."""
	configure(ai_provider="ollama")

	def refuse(*a, **k):
		raise urllib.error.URLError("connection refused")

	monkeypatch.setattr(llm.urllib.request, "urlopen", refuse)
	with pytest.raises(llm.LLMError) as exc:
		llm.generate_json("x", OBJECT_SCHEMA)
	message = str(exc.value)
	assert "Ollama" in message and "running" in message and "11434" in message


def test_a_hosted_provider_being_unreachable_blames_the_connection(monkeypatch):
	configure(ai_provider="openai", ai_key="k")

	def refuse(*a, **k):
		raise urllib.error.URLError("no route")

	monkeypatch.setattr(llm.urllib.request, "urlopen", refuse)
	with pytest.raises(llm.LLMError) as exc:
		llm.generate_json("x", OBJECT_SCHEMA)
	assert "internet" in str(exc.value).lower()
