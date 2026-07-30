"""Where the AI thinking happens — Gemini, or anything speaking OpenAI's API.

Gemini remains the default and nothing about it changes. This adds a second
route for people who would rather use Groq (fast and cheap), OpenAI, or a model
running on their own machine through Ollama or LM Studio — which is the only
way to use the AI features with no account and no key at all.

One HTTP client covers all four, because Groq, Ollama and LM Studio all expose
OpenAI's /chat/completions shape. The differences that remain are the base URL
and whether a key is needed, and both are just settings.

JSON comes back via `response_format: json_object` rather than a strict schema:
OpenAI and Groq support schemas, Ollama and LM Studio mostly do not, and asking
for plain JSON with the shape described in the prompt works on all of them.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import utils

# The presets people actually use. `key` says whether one is required — local
# runners accept anything, so the field is hidden for them in Settings.
PROVIDERS = {
	"gemini": {
		"label": "Google Gemini (default)",
		"base_url": "",
		"model": "gemini-2.5-flash",
		"needs_key": True,
		"local": False,
	},
	"groq": {
		"label": "Groq",
		"base_url": "https://api.groq.com/openai/v1",
		"model": "llama-3.3-70b-versatile",
		"needs_key": True,
		"local": False,
	},
	"openai": {
		"label": "OpenAI",
		"base_url": "https://api.openai.com/v1",
		"model": "gpt-4o-mini",
		"needs_key": True,
		"local": False,
	},
	"ollama": {
		"label": "Ollama (on this PC)",
		"base_url": "http://localhost:11434/v1",
		"model": "llama3.1",
		"needs_key": False,
		"local": True,
	},
	"lmstudio": {
		"label": "LM Studio (on this PC)",
		"base_url": "http://localhost:1234/v1",
		"model": "local-model",
		"needs_key": False,
		"local": True,
	},
}

DEFAULT_PROVIDER = "gemini"

# Local models are slower to first token than a hosted one, and a long
# transcript is a big prompt.
TIMEOUT = 300


class LLMError(Exception):
	"""Message written for the person using the app, not for a log."""


def settings() -> dict:
	"""Which provider is configured, with the preset filled in behind it."""
	cfg = utils.load_config()
	name = cfg.get("ai_provider", DEFAULT_PROVIDER)
	preset = PROVIDERS.get(name, PROVIDERS[DEFAULT_PROVIDER])
	return {
		"provider": name if name in PROVIDERS else DEFAULT_PROVIDER,
		"base_url": (cfg.get("ai_base_url") or preset["base_url"]).rstrip("/"),
		"model": cfg.get("ai_model") or preset["model"],
		"key": cfg.get("ai_key", ""),
		"needs_key": preset["needs_key"],
		"local": preset["local"],
	}


def is_gemini() -> bool:
	return settings()["provider"] == "gemini"


# Google's own endpoint, used only to check a key and list what it can reach.
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Short on purpose: this runs while someone watches a spinner in Settings, and
# "cannot reach it" after eight seconds is a more useful answer than a hang.
TEST_TIMEOUT = 8


def _proxy_opener():
	"""Honour the configured proxy, the same way the downloader does.

	Without this, the customer whose ISP blocks the AI provider gets "key
	rejected" from a test that never left the building — the worst possible
	message, because it sends them to change a key that was fine.
	"""
	proxy = str(utils.load_config().get("proxy", "")).strip()
	if proxy:
		return urllib.request.build_opener(
			urllib.request.ProxyHandler({"http": proxy, "https": proxy})
		)
	return urllib.request.build_opener()


def test(provider: str = "", key: str = "", base_url: str = "", model: str = "") -> dict:
	"""Check a provider answers, before a 40-minute job finds out it does not.

	Everything is optional and falls back to what is saved, so Settings can test
	either the values in the form or the ones already stored. Returns
	{"ok", "message", "models": [...]} and never raises: a failed test is an
	answer, not an exception.

	The model list is the quiet win here. Ollama and LM Studio users guess model
	names — "llama3.1" versus "llama3.1:8b" — and a 404 from a job an hour later
	is a terrible way to learn which one they pulled.
	"""
	saved = settings()
	provider = (provider or saved["provider"]).strip()
	preset = PROVIDERS.get(provider)
	if not preset:
		return {"ok": False, "message": f"Unknown provider: {provider}", "models": []}

	# An empty key means "use the saved one" — the front end never receives the
	# stored key, so it cannot send it back.
	key = key.strip() or (saved["key"] if provider == saved["provider"] else "")
	base_url = (base_url.strip() or preset["base_url"]).rstrip("/")
	model = model.strip() or preset["model"]
	label = preset["label"]

	if preset["needs_key"] and not key:
		return {"ok": False, "message": f"{label} needs an API key.", "models": []}

	if provider == "gemini":
		url = f"{GEMINI_MODELS_URL}?key={urllib.parse.quote(key)}&pageSize=200"
		req = urllib.request.Request(url)
	else:
		req = urllib.request.Request(
			f"{base_url}/models",
			headers={"Authorization": f"Bearer {key or 'not-needed'}"},
		)

	try:
		with _proxy_opener().open(req, timeout=TEST_TIMEOUT) as resp:
			payload = json.loads(resp.read())
	except urllib.error.HTTPError as exc:
		if exc.code in (400, 401, 403):
			return {"ok": False, "message": f"{label} rejected that key.", "models": []}
		return {"ok": False, "message": f"{label} returned an error ({exc.code}).", "models": []}
	except (urllib.error.URLError, TimeoutError, OSError) as exc:
		if preset["local"]:
			return {
				"ok": False,
				"models": [],
				"message": f"Could not reach {label} at {base_url}. Is it running?",
			}
		return {"ok": False, "message": f"Could not reach {label}: {exc}", "models": []}
	except Exception as exc:
		return {"ok": False, "message": f"{label} sent a reply we could not read: {exc}", "models": []}

	models = _model_names(provider, payload)
	message = f"Connected to {label}."
	# Only a warning: providers alias and version model names constantly, and
	# refusing to save over a name we simply did not recognise would be worse
	# than letting the customer proceed.
	if models and model and not any(model in m for m in models):
		message += f" Note: '{model}' is not in its model list."
	return {"ok": True, "message": message, "models": models[:200]}


def _model_names(provider: str, payload: dict) -> list[str]:
	if provider == "gemini":
		# "models/gemini-2.5-flash" -> "gemini-2.5-flash"
		return sorted(
			str(m.get("name", "")).split("/")[-1]
			for m in payload.get("models", [])
			if m.get("name")
		)
	return sorted(str(m.get("id", "")) for m in payload.get("data", []) if m.get("id"))


def _extract_json(text: str):
	"""Models wrap JSON in prose or a ```json fence often enough to plan for it."""
	text = (text or "").strip()
	fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
	if fence:
		text = fence.group(1).strip()
	try:
		return json.loads(text)
	except json.JSONDecodeError:
		pass
	# Last resort: the outermost {...} or [...] in the reply.
	for opener, closer in (("{", "}"), ("[", "]")):
		start, end = text.find(opener), text.rfind(closer)
		if start != -1 and end > start:
			try:
				return json.loads(text[start : end + 1])
			except json.JSONDecodeError:
				continue
	raise LLMError("The model did not return usable JSON. Try a different model.")


def generate_json(prompt: str, schema: dict, temperature: float = 0.4):
	"""Ask an OpenAI-compatible endpoint for JSON matching `schema`.

	`schema` is the same dict the Gemini path uses; here it is described to the
	model in the prompt rather than enforced by the API, so the two routes stay
	interchangeable from the caller's point of view.
	"""
	cfg = settings()
	if cfg["provider"] == "gemini":
		raise LLMError("generate_json is for the OpenAI-compatible providers")
	if cfg["needs_key"] and not cfg["key"]:
		raise LLMError(f"Add an API key for {PROVIDERS[cfg['provider']]['label']} in Settings.")

	instruction = (
		"Reply with JSON only — no prose, no markdown fence. "
		"It must match this JSON Schema exactly:\n"
		f"{json.dumps(schema)}"
	)
	body = {
		"model": cfg["model"],
		"temperature": temperature,
		"response_format": {"type": "json_object"},
		"messages": [
			{"role": "system", "content": instruction},
			{"role": "user", "content": prompt},
		],
	}

	req = urllib.request.Request(
		f"{cfg['base_url']}/chat/completions",
		data=json.dumps(body).encode(),
		headers={
			"Content-Type": "application/json",
			"Authorization": f"Bearer {cfg['key'] or 'not-needed'}",
		},
		method="POST",
	)

	try:
		with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
			payload = json.loads(resp.read())
	except urllib.error.HTTPError as exc:
		detail = ""
		try:
			detail = json.loads(exc.read()).get("error", {}).get("message", "")
		except Exception:
			pass
		if exc.code in (401, 403):
			raise LLMError("The AI provider rejected your key. Check it in Settings.") from exc
		if exc.code == 404:
			raise LLMError(
				f"Model '{cfg['model']}' was not found on that provider. Check the model name in Settings."
			) from exc
		raise LLMError(detail or f"The AI provider returned an error ({exc.code}).") from exc
	except (urllib.error.URLError, TimeoutError, OSError) as exc:
		if cfg["local"]:
			raise LLMError(
				f"Could not reach {PROVIDERS[cfg['provider']]['label']} at {cfg['base_url']}. "
				"Is it running?"
			) from exc
		raise LLMError("Could not reach the AI provider. Check your internet connection.") from exc

	try:
		content = payload["choices"][0]["message"]["content"]
	except (KeyError, IndexError) as exc:
		raise LLMError("The AI provider sent a reply we could not read.") from exc

	data = _extract_json(content)

	# Gemini returns a bare list for the moments schema; some models wrap it in
	# an object because JSON mode insists on one. Unwrap a single list value.
	if schema.get("type") == "array" and isinstance(data, dict):
		for value in data.values():
			if isinstance(value, list):
				return value
	return data
