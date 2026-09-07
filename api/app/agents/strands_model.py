"""Strands ``Model`` provider bridging to our thread-safe ``BedrockClient``.

Translates Strands' message/stream/tool protocol ⇄ the OpenAI-style body that
``BedrockClient.invoke`` expects (gpt-oss-120b served OpenAI-compatible on Bedrock),
including tool-call conversion in both directions so the agent loop works normally.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import AsyncIterable

from strands.models import Model


def _strip_reasoning(text):
    if not text:
        return text
    return re.sub(r"<reasoning>.*?</reasoning>", "", text,
                  flags=re.DOTALL | re.IGNORECASE).strip()


def to_openai_tools(tool_specs):
    if not tool_specs:
        return None
    tools = []
    for ts in tool_specs:
        schema = ts.get("inputSchema", {}) or {}
        params = schema.get("json", schema) if isinstance(schema, dict) else {}
        tools.append({"type": "function", "function": {
            "name": ts["name"], "description": ts.get("description", ""),
            "parameters": params}})
    return tools


def to_openai_tool_choice(tool_choice, openai_tools):
    if not tool_choice or not openai_tools:
        return None
    if "any" in tool_choice:
        return "required"
    if "tool" in tool_choice:
        name = tool_choice["tool"].get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    return "auto"


def _tool_result_text(tool_result):
    parts = []
    for c in tool_result.get("content", []) or []:
        if "text" in c:
            parts.append(c["text"])
        elif "json" in c:
            parts.append(json.dumps(c["json"]))
    return "\n".join(parts)


def _repair_tool_pairs(messages):
    assistant_ids = {tc["id"] for m in messages if m.get("role") == "assistant"
                     for tc in m.get("tool_calls", [])}
    result_ids = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
    repaired = []
    for m in messages:
        if m.get("role") == "tool":
            if m["tool_call_id"] in assistant_ids:
                repaired.append(m)
        elif m.get("role") == "assistant" and m.get("tool_calls"):
            kept = [tc for tc in m["tool_calls"] if tc["id"] in result_ids]
            if kept:
                nm = dict(m); nm["tool_calls"] = kept; repaired.append(nm)
            elif m.get("content"):
                nm = dict(m); nm.pop("tool_calls", None); repaired.append(nm)
        else:
            repaired.append(m)
    return repaired


def to_openai_messages(messages, system_prompt=None):
    out = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    for msg in messages:
        role = msg.get("role", "user")
        text_parts, tool_calls, tool_results = [], [], []
        for block in msg.get("content", []) or []:
            if "text" in block:
                text_parts.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                tool_calls.append({"id": tu["toolUseId"], "type": "function",
                                   "function": {"name": tu["name"],
                                                "arguments": json.dumps(tu.get("input", {}))}})
            elif "toolResult" in block:
                tr = block["toolResult"]
                tool_results.append({"role": "tool", "tool_call_id": tr["toolUseId"],
                                     "content": _tool_result_text(tr)})
        if role == "assistant":
            m = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else None}
            if tool_calls:
                m["tool_calls"] = tool_calls
            out.append(m)
        else:
            if text_parts:
                out.append({"role": "user", "content": "\n".join(text_parts)})
            out.extend(tool_results)
    return _repair_tool_pairs(out)


def message_to_events(message):
    events = [{"messageStart": {"role": "assistant"}}]
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", {})
            tuid = tc.get("id") or f"tooluse_{uuid.uuid4().hex[:16]}"
            args = fn.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args or {})
            events.append({"contentBlockStart": {"start": {"toolUse": {
                "toolUseId": tuid, "name": fn.get("name", "")}}}})
            events.append({"contentBlockDelta": {"delta": {"toolUse": {"input": args}}}})
            events.append({"contentBlockStop": {}})
        stop_reason = "tool_use"
    else:
        text = _strip_reasoning(message.get("content") or "")
        events.append({"contentBlockDelta": {"delta": {"text": text}}})
        events.append({"contentBlockStop": {}})
        stop_reason = "end_turn"
    events.append({"messageStop": {"stopReason": stop_reason}})
    events.append({"metadata": {"usage": {"inputTokens": 0, "outputTokens": 0,
                                          "totalTokens": 0}, "metrics": {"latencyMs": 0}}})
    return events


class StrandsBedrockModel(Model):
    def __init__(self, client, *, max_tokens=2600, temperature=0.0):
        self.client = client
        self.config = {"model_id": getattr(client, "model_id", None),
                       "max_tokens": max_tokens, "temperature": temperature}

    def get_config(self):
        return self.config

    def update_config(self, **kwargs):
        self.config.update(kwargs)

    async def stream(self, messages, tool_specs=None, system_prompt=None, *,
                     tool_choice=None, **kwargs) -> AsyncIterable[dict]:
        openai_messages = to_openai_messages(messages, system_prompt)
        openai_tools = to_openai_tools(tool_specs)
        oai_tool_choice = to_openai_tool_choice(tool_choice, openai_tools)
        message = await asyncio.to_thread(
            self.client.invoke, openai_messages, tools=openai_tools,
            max_tokens=self.config["max_tokens"],
            temperature=self.config["temperature"], tool_choice=oai_tool_choice)
        for event in message_to_events(message):
            yield event

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        from strands.event_loop import streaming
        from strands.tools import convert_pydantic_to_tool_spec
        tool_spec = convert_pydantic_to_tool_spec(output_model)
        response = self.stream(messages=prompt, tool_specs=[tool_spec],
                               system_prompt=system_prompt, tool_choice={"any": {}}, **kwargs)
        event = None
        async for event in streaming.process_stream(response):
            yield event
        if event is None or "stop" not in event:
            raise ValueError("empty/incomplete stream from model")
        stop_reason, message, _, _ = event["stop"]
        if stop_reason != "tool_use":
            raise ValueError(f'stop_reason {stop_reason} != tool_use')
        output_response = None
        for block in message["content"]:
            if block.get("toolUse") and block["toolUse"]["name"] == tool_spec["name"]:
                output_response = block["toolUse"]["input"]
        if output_response is None:
            raise ValueError("no structured-output tool use in response")
        yield {"output": output_model(**output_response)}
