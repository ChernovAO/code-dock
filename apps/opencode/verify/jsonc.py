"""Разбор JSONC: комментарии `//` и `/* */`, висячие запятые."""

import json
from typing import Any


def loads(text: str) -> Any:
    out: list[str] = []
    in_string = False
    last_significant = -1
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < len(text):
                i += 1
                out.append(text[i])
            elif ch == '"':
                in_string = False
        elif text.startswith("//", i):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end == -1 else end + 2
            continue
        else:
            if ch in "}]" and last_significant >= 0 and out[last_significant] == ",":
                out[last_significant] = ""
            out.append(ch)
            if not ch.isspace():
                last_significant = len(out) - 1
            in_string = ch == '"'
        i += 1
    return json.loads("".join(out))
