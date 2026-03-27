#!/usr/bin/env python3
"""
kadal morning briefing — daily status report at 7am
sends comprehensive system health + activity summary to Telegram
"""

import json
import os
import sys
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
import requests

load_dotenv(os.path.expanduser("~/.env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# === CONFIG ===
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
USER_ID = int(os.environ.get("TELEGRAM_USER_ID", "0"))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

CONVERSATIONS_FILE = Path.home() / "training" / "conversations" / "conversations.jsonl"
POSTS_DIR = Path.home() / "kadal" / "content" / "posts"
KADAL_LOG = Path.home() / "kadal" / "kadal.log"
FINETUNE_LOG = Path.home() / "training" / "finetune.log"

# === METRICS ===

def get_anthropic_usage() -> dict:
    """Estimate Anthropic API usage since yesterday (rough calculation)."""
    try:
        # Check recent kadal logs for Claude calls
        if not KADAL_LOG.exists():
            return {"calls": 0, "estimated_tokens": 0, "estimated_cost": "$0.00"}
        
        logs = KADAL_LOG.read_text()
        claude_calls = logs.count("Calling Claude")
        
        # Very rough: assume 3k output tokens per blog post, 5k input
        tokens_per_call = 8000
        total_tokens = claude_calls * tokens_per_call
        
        # Sonnet pricing: ~$3/M input, ~$15/M output (rough avg = $9/M)
        cost = (total_tokens / 1_000_000) * 9
        
        return {
            "calls": claude_calls,
            "estimated_tokens": total_tokens,
            "estimated_cost": f"${cost:.2f}"
        }
    except Exception as e:
        log.warning(f"Could not estimate Anthropic usage: {e}")
        return {"calls": 0, "estimated_tokens": 0, "estimated_cost": "$?"}

def get_conversations_24h() -> int:
    """Count new conversations in last 24 hours."""
    if not CONVERSATIONS_FILE.exists():
        return 0
    
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    count = 0
    with open(CONVERSATIONS_FILE, "r") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                ts = datetime.fromisoformat(entry["timestamp"])
                if ts >= cutoff:
                    count += 1
            except:
                pass
    return count

def get_posts_yesterday() -> int:
    """Count blog posts published yesterday."""
    if not POSTS_DIR.exists():
        return 0
    
    yesterday = datetime.now() - timedelta(days=1)
    yesterday_str = yesterday.strftime("%Y-%m-%d")
    
    count = 0
    for post in POSTS_DIR.glob("*.md"):
        if yesterday_str in post.name:
            count += 1
    return count

def get_next_posts() -> list:
    """Get times of next 3 scheduled posts (8am, 2pm, 8pm)."""
    now = datetime.now()
    posts = []
    
    # Today's posts
    for hour in [8, 14, 20]:
        post_time = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if post_time > now:
            posts.append(post_time)
    
    # If all today's posts have passed, show tomorrow's
    if len(posts) < 3:
        tomorrow = now + timedelta(days=1)
        for hour in [8, 14, 20]:
            post_time = tomorrow.replace(hour=hour, minute=0, second=0, microsecond=0)
            posts.append(post_time)
    
    return sorted(posts)[:3]

def get_ollama_status() -> dict:
    """Get current loaded model and memory usage."""
    try:
        result = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return {"model": "none loaded", "memory": "unknown"}
        
        lines = result.stdout.strip().split("\n")
        if len(lines) > 1:
            # Format: NAME ID SIZE PROCESSOR UNTIL
            parts = lines[1].split()
            model = parts[0] if parts else "unknown"
            size = parts[2] if len(parts) > 2 else "unknown"
            return {"model": model, "memory": size}
        return {"model": "none", "memory": "0B"}
    except Exception as e:
        log.warning(f"Could not get Ollama status: {e}")
        return {"model": "unknown", "memory": "unknown"}

def get_recent_errors() -> str:
    """Extract recent errors from logs."""
    errors = []
    
    for log_file in [KADAL_LOG, FINETUNE_LOG]:
        if not log_file.exists():
            continue
        
        with open(log_file, "r") as f:
            for line in f.readlines()[-50:]:  # last 50 lines
                if "error" in line.lower() or "failed" in line.lower() or "traceback" in line.lower():
                    errors.append(line.strip())
    
    if not errors:
        return "none"
    
    return "\n".join(errors[-3:])  # last 3 errors

def send_telegram_message(text: str):
    """Send message to user via Telegram bot."""
    if not BOT_TOKEN or not USER_ID:
        log.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_USER_ID in ~/.env")
        return False
    
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": USER_ID,
        "text": text,
        "parse_mode": "markdown"
    }
    
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            log.info("Telegram message sent successfully")
            return True
        else:
            log.error(f"Telegram API error: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        log.error(f"Failed to send Telegram message: {e}")
        return False

def format_briefing() -> str:
    """Generate the morning briefing message."""
    # Gather metrics
    usage = get_anthropic_usage()
    convos_24h = get_conversations_24h()
    posts_yesterday = get_posts_yesterday()
    next_posts = get_next_posts()
    ollama = get_ollama_status()
    errors = get_recent_errors()
    
    # Format times
    next_times = "\n".join([p.strftime("%I:%M %p") for p in next_posts])
    
    message = f"""*kadal morning briefing* ☕

📊 *api usage* (since yesterday)
  claude calls: {usage['calls']}
  estimated tokens: {usage['estimated_tokens']:,}
  estimated cost: {usage['estimated_cost']}

💬 *conversations logged*
  last 24h: {convos_24h}

📝 *blog activity*
  posts yesterday: {posts_yesterday}
  next 3 posts:
{next_times}

🤖 *ollama status*
  loaded: {ollama['model']}
  memory: {ollama['memory']}

⚠️ *recent errors*
  {errors}

—
generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} utc
"""
    return message

def main():
    log.info("=== kadal morning briefing starting ===")
    
    briefing = format_briefing()
    log.info(f"Briefing:\n{briefing}")
    
    sent = send_telegram_message(briefing)
    if sent:
        log.info("✅ Briefing sent to Telegram")
    else:
        log.error("❌ Failed to send briefing")
        sys.exit(1)

if __name__ == "__main__":
    main()
