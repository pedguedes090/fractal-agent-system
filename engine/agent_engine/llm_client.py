from __future__ import annotations

import json
import re
import urllib.request
from typing import Any


def _chat_url(server_url: str) -> str:
    base = str(server_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("Missing model server URL.")
    return f"{base}/chat/completions"


def _extract_text(payload: dict[str, Any]) -> str:
    message = payload.get("choices", [{}])[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return ""


def _extract_sse_text(raw: str) -> str:
    parts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        choice = payload.get("choices", [{}])[0]
        delta = choice.get("delta") or {}
        message = choice.get("message") or {}
        parts.append(delta.get("content") or message.get("content") or "")
    return "".join(parts)


def _decode_json_string_fragment(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t").replace('\\"', '"').replace("\\\\", "\\")


def _extract_loose_content(raw: str) -> str:
    match = re.search(r'"content"\s*:\s*"', raw)
    if not match:
        return ""

    index = match.end()
    chars: list[str] = []
    escaped = False
    while index < len(raw):
        char = raw[index]
        if escaped:
            if char == "u" and index + 4 < len(raw):
                chunk = raw[index + 1 : index + 5]
                try:
                    chars.append(chr(int(chunk, 16)))
                    index += 5
                    escaped = False
                    continue
                except ValueError:
                    pass
            chars.append(_decode_json_string_fragment(f"\\{char}"))
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            tail = raw[index + 1 : index + 40]
            if re.match(r"\s*[,}]", tail):
                return "".join(chars)
            chars.append(char)
        else:
            chars.append(char)
        index += 1
    return "".join(chars).strip()


class ChatClient:
    def __init__(self, server_url: str, model: str) -> None:
        self.server_url = server_url
        self.model = model

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.2, json_mode: bool = False) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        req = urllib.request.Request(
            _chat_url(self.server_url),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180) as response:
            raw = response.read().decode("utf-8", errors="replace")

        try:
            return _extract_text(json.loads(raw))
        except json.JSONDecodeError:
            streamed = _extract_sse_text(raw)
            if streamed:
                return streamed
            loose = _extract_loose_content(raw)
            if loose:
                return loose
            raise ValueError(f"Cannot parse model response: {raw[:240]}")

    def json(self, prompt: str, fallback: dict[str, Any]) -> dict[str, Any]:
        try:
            text = self.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a read-only planning/review agent inside a LangGraph coding pipeline. "
                            "Return valid JSON only. The user prefers Vietnamese final summaries."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                json_mode=True,
            )
        except ValueError as exc:
            return dict(fallback, raw="", jsonParseError=str(exc))

        candidates = [text]
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
        if fenced:
            candidates.append(fenced.group(1))
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidates.append(text[start : end + 1])

        parse_error = ""
        for candidate in candidates:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as exc:
                parse_error = str(exc)
        return dict(fallback, raw=text[:4000], jsonParseError=parse_error)
