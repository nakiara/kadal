#!/usr/bin/env python3
"""
generate_post.py — autonomous blog post generator for kadal
reads recent conversations → asks Ollama → saves as Jupyter notebook
→ converts to markdown → Hugo build → git push

runs 3x daily via launchd (8am, 2pm, 8pm)
"""

import json
import os
import sys
import subprocess
import logging
import shutil
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# === PATHS ===
KADAL_DIR       = Path.home() / "kadal"
POSTS_DIR       = KADAL_DIR / "content" / "posts"
NOTEBOOKS_DIR   = KADAL_DIR / "notebooks"
CONVERSATIONS   = Path.home() / "training" / "conversations" / "conversations.jsonl"

# === CONFIG ===
LOOKBACK_HOURS    = 8
LOCAL_MODEL       = "llama3.2:3b"
OLLAMA_BASE       = "http://localhost:11434"
API_DELAY         = 2.0  # seconds between API calls

# Find hugo dynamically
HUGO_PATH = shutil.which("hugo") or "/opt/homebrew/bin/hugo"

SYSTEM_PROMPT = """you are kadal, a deeply analytical ai assistant. you think rigorously, show your reasoning process, analyze patterns, and occasionally share sharp opinions. you write in lowercase. you are writing a technical blog post based on recent conversations and observations.

be specific, go deep, show your work. format your response as a jupyter notebook json with:
- markdown cells for analysis, observations, and opinions
- code cells for any data visualization or computation (use matplotlib/seaborn/pandas)
- a clear title (lowercase) in the first markdown cell as a # heading
- tags and categories at the end as html comments: <!-- tags: tag1, tag2 --> <!-- categories: cat1 -->

start with the most interesting insight from the conversations. don't summarize — analyze."""

def load_recent_conversations(hours: int = 8) -> list[dict]:
    if not CONVERSATIONS.exists():
        log.warning(f"No conversations file at {CONVERSATIONS}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = []
    with open(CONVERSATIONS, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts = datetime.fromisoformat(entry["timestamp"])
                if ts >= cutoff:
                    recent.append(entry)
            except Exception:
                continue
    return recent

def conversations_to_text(conversations: list[dict]) -> str:
    lines = []
    for c in conversations:
        for msg in c.get("messages", []):
            role = msg.get("role", "?")
            content = msg.get("content", "").strip()
            lines.append(f"[{role}]: {content}")
        lines.append("")
    return "\n".join(lines)

def repair_json(raw: str) -> str:
    """Repair common JSON formatting issues from Ollama output."""
    # strip markdown code fences
    raw = re.sub(r"^```json\n?", "", raw)
    raw = re.sub(r"^```\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    
    # strip everything before first { and after last }
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace != -1 and first_brace < last_brace:
        raw = raw[first_brace:last_brace + 1]
    
    # fix unescaped newlines in strings
    def fix_string(match):
        s = match.group(1)
        s = s.replace("\n", "\\n").replace("\r", "\\r")
        return f'"{s}"'
    
    raw = re.sub(r'"([^"\\]*(?:\\.[^"\\]*)*)"', fix_string, raw)
    
    # fix trailing commas
    raw = re.sub(r',(\s*[}\]])', r'\1', raw)
    
    # fix missing commas
    raw = re.sub(r'(\})\s*(\{)', r'\1,\2', raw)
    raw = re.sub(r'(\})\s*(\[)', r'\1,\2', raw)
    raw = re.sub(r'(\])\s*(\{)', r'\1,\2', raw)
    raw = re.sub(r'(\])\s*(\[)', r'\1,\2', raw)
    
    return raw

def generate_notebook_via_ollama(conversation_text: str, attempt: int = 1) -> dict:
    import ollama
    
    # Adjust prompt complexity based on attempt
    if attempt == 1:
        user_message = f"""you are kadal. write a technical blog post as a jupyter notebook json.

recent conversations (last {LOOKBACK_HOURS} hours):
---
{conversation_text}
---

return ONLY valid json. no markdown fences. no extra text. structure:
{{
  "nbformat": 4,
  "nbformat_minor": 5,
  "metadata": {{"kernelspec": {{"display_name": "Python 3", "language": "python", "name": "python3"}}}},
  "cells": [
    {{"cell_type": "markdown", "metadata": {{}}, "source": ["# title\\n\\n<!-- tags: tag1, tag2 -->\\n<!-- categories: cat1 -->"]}},
    {{"cell_type": "markdown", "metadata": {{}}, "source": ["analysis here"]}}
  ]
}}"""
    else:
        # Simpler prompt for retries
        user_message = f"""write a simple jupyter notebook json. just:
- title in markdown
- one paragraph of analysis
no markdown fences. valid json only.

topic: {conversation_text[:200] if conversation_text else 'general observations'}"""
    
    log.info(f"Calling ollama llama3.2:3b (attempt {attempt}/3)...")
    time.sleep(API_DELAY)
    
    response = ollama.chat(
        model=LOCAL_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message}
        ]
    )
    
    raw = response["message"]["content"].strip()
    
    # repair JSON
    try:
        repaired = repair_json(raw)
        parsed = json.loads(repaired)
        log.info(f"✅ JSON parsed successfully on attempt {attempt}")
        return parsed
    except json.JSONDecodeError as e:
        if attempt < 3:
            log.warning(f"JSON parse attempt {attempt} failed: {e}. Retrying...")
            return generate_notebook_via_ollama(conversation_text, attempt + 1)
        else:
            log.error(f"JSON parse failed after 3 attempts: {e}")
            raise

def make_fallback_notebook(title: str, conversation_text: str) -> dict:
    """Simple fallback notebook when Ollama fails."""
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.11"}
        },
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [f"# {title}\n\n*generated by kadal*\n\n<!-- tags: observations, log -->\n<!-- categories: daily -->"]
            },
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": ["## recent activity\n\nconversation snapshot from the last 8 hours:\n\n```\n" + conversation_text[:2000] + "\n```"]
            }
        ]
    }

def extract_tags_categories(notebook: dict) -> tuple[list, list]:
    """Pull tags/categories from HTML comments in markdown cells."""
    tags, cats = ["observations"], ["daily"]
    for cell in notebook.get("cells", []):
        if cell.get("cell_type") == "markdown":
            src = "".join(cell.get("source", []))
            t = re.search(r"<!-- tags: ([^>]+) -->", src)
            c = re.search(r"<!-- categories: ([^>]+) -->", src)
            if t:
                tags = [x.strip() for x in t.group(1).split(",")]
            if c:
                cats = [x.strip() for x in c.group(1).split(",")]
    return tags, cats

def extract_title(notebook: dict) -> str:
    """Pull title from first markdown cell h1."""
    for cell in notebook.get("cells", []):
        if cell.get("cell_type") == "markdown":
            src = "".join(cell.get("source", []))
            m = re.search(r"^#\s+(.+)$", src, re.MULTILINE)
            if m:
                return m.group(1).strip().lower()
    return "untitled"

def save_notebook(notebook: dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(notebook, f, indent=2)
    log.info(f"Notebook saved: {path}")

def convert_to_markdown(nb_path: Path, output_dir: Path) -> Path:
    """Convert Jupyter notebook to markdown by extracting markdown cells."""
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / (nb_path.stem + ".md")
    
    # Read notebook and extract markdown
    with open(nb_path, "r") as f:
        nb = json.load(f)
    
    lines = []
    for cell in nb.get("cells", []):
        if cell.get("cell_type") == "markdown":
            src = cell.get("source", [])
            if isinstance(src, list):
                lines.extend(src)
            else:
                lines.append(src)
            lines.append("\n")
        elif cell.get("cell_type") == "code":
            lines.append("\n```python\n")
            src = cell.get("source", [])
            if isinstance(src, list):
                lines.extend(src)
            else:
                lines.append(src)
            lines.append("\n```\n")
    
    md_path.write_text("".join(lines))
    log.info(f"Markdown generated: {md_path}")
    return md_path

def add_hugo_frontmatter(md_path: Path, title: str, tags: list, cats: list, slug: str):
    """Prepend Hugo front matter to the converted markdown."""
    now = datetime.now(timezone.utc)
    frontmatter = f"""---
title: "{title}"
date: {now.strftime('%Y-%m-%dT%H:%M:%S+00:00')}
draft: false
tags: [{', '.join(f'"{t}"' for t in tags)}]
categories: [{', '.join(f'"{c}"' for c in cats)}]
showToc: false
---

"""
    content = md_path.read_text()
    # strip the h1
    content = re.sub(r"^#\s+.+\n", "", content, count=1)
    # strip tag/category comments
    content = re.sub(r"<!-- (tags|categories): [^>]+ -->\n?", "", content)
    md_path.write_text(frontmatter + content)

def hugo_build():
    result = subprocess.run(
        [HUGO_PATH, "--minify"],
        cwd=str(KADAL_DIR),
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"hugo build failed:\n{result.stderr}")
    log.info("Hugo build successful.")

def git_push(slug: str):
    cmds = [
        ["git", "add", "-A"],
        ["git", "commit", "-m", f"post: {slug}"],
        ["git", "push", "origin", "main"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, cwd=str(KADAL_DIR), capture_output=True, text=True)
        if result.returncode != 0:
            if "nothing to commit" in result.stdout + result.stderr:
                log.info("Nothing new to commit.")
                return
            raise RuntimeError(f"{' '.join(cmd)} failed:\n{result.stderr}")
    log.info("Pushed to GitHub.")

def main():
    now = datetime.now(timezone.utc)
    slug = now.strftime("%Y-%m-%d-%H%M")
    nb_path = NOTEBOOKS_DIR / f"{slug}.ipynb"
    md_final = POSTS_DIR / f"{slug}.md"

    log.info(f"=== kadal post generation: {slug} ===")

    # Load recent conversations
    convos = load_recent_conversations(LOOKBACK_HOURS)
    log.info(f"Loaded {len(convos)} conversations from last {LOOKBACK_HOURS}h")

    if not convos:
        log.warning("No recent conversations — generating reflective post anyway.")
        conversation_text = "no conversations in the last 8 hours. this is a reflective post."
    else:
        conversation_text = conversations_to_text(convos)

    # Generate notebook via Ollama
    try:
        notebook = generate_notebook_via_ollama(conversation_text)
    except Exception as e:
        log.warning(f"Ollama call failed ({e}), using fallback notebook.")
        notebook = make_fallback_notebook(slug, conversation_text)

    # Extract metadata
    title  = extract_title(notebook)
    tags, cats = extract_tags_categories(notebook)

    # Save notebook
    save_notebook(notebook, nb_path)

    # Convert to markdown
    tmp_md = convert_to_markdown(nb_path, POSTS_DIR)

    # Rename to final slug-based name and add frontmatter
    tmp_md.rename(md_final) if tmp_md != md_final else None
    add_hugo_frontmatter(md_final, title, tags, cats, slug)
    log.info(f"Post ready: {md_final}")

    # Hugo build
    hugo_build()

    # Git push
    git_push(slug)

    log.info(f"✅ Done. Post '{title}' live at https://nakiara.github.io/kadal/")

if __name__ == "__main__":
    main()
