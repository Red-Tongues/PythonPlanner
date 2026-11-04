"""Send a review brief (plus attached files) to Gemini and save the reply.

Panel tooling for the review rounds (R2 onward), so the engineer session can consult Gemini
directly instead of relaying through the browser. Stateless: one self-contained request per call.

Usage:
    GEMINI_API_KEY=... python scripts/ask_gemini.py BRIEF.md [ATTACHMENT ...] [-o reply.md]

Environment:
    GEMINI_API_KEY  required (stored as an environment secret, never committed)
    GEMINI_MODEL    optional; default: the newest available "pro" model the key can use
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import httpx

API = "https://generativelanguage.googleapis.com/v1beta"


def _headers() -> dict[str, str]:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY is not set (add it as an environment secret)")
    return {"x-goog-api-key": key}


def pick_model(client: httpx.Client) -> str:
    if os.environ.get("GEMINI_MODEL"):
        return os.environ["GEMINI_MODEL"]
    data = client.get(f"{API}/models", params={"pageSize": 200}).raise_for_status().json()
    usable = [
        m["name"].removeprefix("models/")
        for m in data.get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]
    pro = [m for m in usable if "pro" in m and not re.search(r"vision|tts|image|embed", m)]

    def version(name: str) -> tuple[float, int]:
        found = re.search(r"(\d+(?:\.\d+)?)", name)
        return (float(found.group(1)) if found else 0.0, "preview" not in name)

    choices = sorted(pro or usable, key=version)
    if not choices:
        sys.exit("no model with generateContent is available to this key")
    return choices[-1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("brief", type=Path)
    parser.add_argument("attachments", nargs="*", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()

    parts = [{"text": args.brief.read_text()}]
    for path in args.attachments:
        parts.append({"text": f"\n\n===== ATTACHMENT: {path.name} =====\n{path.read_text()}"})
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.2},
    }

    with httpx.Client(headers=_headers(), timeout=600) as client:
        model = pick_model(client)
        response = client.post(f"{API}/models/{model}:generateContent", json=body)
        if response.status_code != 200:
            sys.exit(f"Gemini error {response.status_code}: {response.text[:500]}")
        data = response.json()
    candidates = data.get("candidates") or []
    text = "".join(
        p.get("text", "") for p in (candidates[0]["content"]["parts"] if candidates else [])
    )
    usage = data.get("usageMetadata", {})
    header = (
        f"<!-- model: {model}; prompt tokens: {usage.get('promptTokenCount')}; "
        f"reply tokens: {usage.get('candidatesTokenCount')} -->\n"
    )
    if args.output:
        args.output.write_text(header + text)
        print(f"saved {args.output} ({model})")
    else:
        print(header + text)


if __name__ == "__main__":
    main()
