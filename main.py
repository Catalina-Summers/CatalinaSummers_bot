#!/usr/bin/env python3
"""
Ani — Telegram agent bot.

Provider-agnostic: any OpenAI-compatible /chat/completions host.
Reads GROQ_API_KEY automatically (or API_KEY + API_BASE).

Loop safety: one execution per unique tool call, hard tool budget, wall-clock
timeout, single nudge, busy lock, update de-duplication.

Voice safety: anti-hedge prompt block, non-prudish model defaults, reply
continuation so answers never stop mid-sentence.
"""

import asyncio
import base64
import hashlib
import html as html_mod
import importlib.util
import io
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

import httpx
from aiohttp import web
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from telegram import BotCommand, InputFile, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    import imageio_ffmpeg
except Exception:  # noqa: BLE001
    imageio_ffmpeg = None


# ─────────────────────────────── config ───────────────────────────────

def _env(k: str, d: str | None = None, required: bool = False) -> str:
    v = os.environ.get(k, d)
    if required and not v:
        raise SystemExit(f"[fatal] missing env var: {k}")
    return v or ""


BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", required=True)

GROQ_KEY = _env("GROQ_API_KEY", "")
API_KEY = _env("API_KEY", "") or GROQ_KEY or _env("VENICE_API_KEY", "")
if not API_KEY:
    raise SystemExit("[fatal] missing env var: API_KEY or GROQ_API_KEY")

_DEFAULT_BASE = "https://api.groq.com/openai/v1" if GROQ_KEY and not _env("API_BASE", "") else ""
API_BASE = (_env("API_BASE", "") or _env("VENICE_BASE_URL", "") or _DEFAULT_BASE).rstrip("/")
if not API_BASE:
    raise SystemExit("[fatal] set API_BASE (e.g. https://api.groq.com/openai/v1)")
IS_VENICE = "venice.ai" in API_BASE

OWNER_ID = int(_env("OWNER_ID", "0") or 0)
BOT_NAME = _env("BOT_NAME", "Ani")
TEXT_MODEL = _env("TEXT_MODEL", "")
ROUTER_MODEL = _env("ROUTER_MODEL", "")
VISION_MODEL = _env("VISION_MODEL", "")
IMAGE_MODEL = _env("IMAGE_MODEL", "")
VIDEO_MODEL = _env("VIDEO_MODEL", "")

REPLY_TOKENS = int(_env("REPLY_TOKENS", "1200"))
TEMPERATURE = float(_env("TEMPERATURE", "0.85"))
HEARTBEAT_MIN = int(_env("HEARTBEAT_MINUTES", "45"))
MAX_STEPS = int(_env("MAX_AGENT_STEPS", "8"))
MAX_TOOL_CALLS = int(_env("MAX_TOOL_CALLS", "10"))
TURN_TIMEOUT_S = int(_env("TURN_TIMEOUT_S", "150"))
HISTORY_N = int(_env("HISTORY_MESSAGES", "18"))
PY_TIMEOUT = int(_env("PY_TIMEOUT", "120"))
VIDEO_FRAMES = int(_env("VIDEO_FRAMES", "6"))
AUDIO_MAX_S = int(_env("AUDIO_MAX_SECONDS", "180"))
MAX_DOWNLOAD_MB = int(_env("MAX_DOWNLOAD_MB", "20"))
MEDIA_INLINE = _env("MEDIA_INLINE", "1") == "1"
SHOW_PIPELINE = _env("SHOW_PIPELINE", "1") == "1"
PORT = int(_env("PORT", "8080"))
WEBHOOK_URL = _env("WEBHOOK_URL", "").rstrip("/")

ROOT = Path(__file__).resolve().parent
DATA = Path(_env("DATA_DIR", str(ROOT / "data")))
WORKSPACE = Path(_env("WORKSPACE", str(ROOT / "workspace")))
PLUGINS = Path(_env("PLUGIN_DIR", str(ROOT / "plugins")))
IDENTITY = DATA / "identity"
MEDIA = DATA / "media"
for _d in (DATA, WORKSPACE, PLUGINS, IDENTITY, MEDIA):
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA / "her.db"
PERSONA_FILE = Path(_env("PERSONA_FILE", str(ROOT / "persona.md")))

_VAULT_SEED = _env("VAULT_KEY", "") or (API_KEY + BOT_TOKEN)
FERNET = Fernet(base64.urlsafe_b64encode(hashlib.sha256(_VAULT_SEED.encode()).digest()))

BUSY: set[int] = set()
SEEN_UPDATES: deque = deque(maxlen=400)

NUDGE = (
    "You replied without calling a single tool. This turn needs real work. "
    "Call the right tool now — do not narrate, do not mention wifi or internet problems. "
    "If a tool fails you will be told the error; call a different tool or report the exact error."
)

TASK_HINT = re.compile(
    r"\b(search|look up|google|find|check|fetch|scrape|browse|download|"
    r"generate|draw|make me|create|build|code|script|install|run|execute|"
    r"remind|schedule|save|remember|store|login|log in|api|http|"
    r"image|picture|photo of|video|render|convert|extract)\b",
    re.I,
)

NL_COMMANDS = {
    "doctor": "cmd_doctor", "build": "cmd_build", "models": "cmd_models",
    "help": "cmd_help", "start": "cmd_start", "reset": "cmd_reset",
    "model": "cmd_model", "persona": "cmd_persona",
}

# Deliberately ordered least-prudish first. llama-3.3-70b is the one that
# answers "i'm not comfortable with that".
TEXT_PREFS = [
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "openai/gpt-oss-120b",
    "qwen/qwen3-32b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama-3.3-70b-versatile",
]
VISION_PREFS = [
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama-3.2-90b-vision-preview",
    "llama-3.2-11b-vision-preview",
]


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_s() -> str:
    return now().isoformat(timespec="seconds")


def log(*a) -> None:
    print("[ani]", *a, flush=True)


def compute_build_id() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    except Exception:  # noqa: BLE001
        return "unknown"


BUILD_ID = compute_build_id()


# ─────────────────────────────── storage ───────────────────────────────

class Store:
    def __init__(self, path: Path):
        self._lock = Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, role TEXT, content TEXT, ts TEXT);
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                k TEXT, v TEXT, ts TEXT);
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT, details TEXT, due_ts TEXT, status TEXT, ts TEXT);
            CREATE TABLE IF NOT EXISTS creds (name TEXT PRIMARY KEY, blob TEXT, ts TEXT);
            """
        )
        self.db.commit()

    def _x(self, sql, args=()):
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def add_message(self, chat_id: int, role: str, content: str) -> None:
        self._x("INSERT INTO messages (chat_id, role, content, ts) VALUES (?,?,?,?)",
                (chat_id, role, content, now_s()))

    def recent_messages(self, chat_id: int, limit: int = 18) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT role, content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, limit)).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def clear(self, chat_id: int) -> None:
        self._x("DELETE FROM messages WHERE chat_id=?", (chat_id,))

    def kv_set(self, k: str, v: str) -> None:
        self._x("INSERT INTO kv (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def kv_get(self, k: str, d: str = "") -> str:
        with self._lock:
            r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else d

    def remember(self, k: str, v: str) -> None:
        self._x("INSERT INTO memories (k,v,ts) VALUES (?,?,?)", (k, v, now_s()))

    def recall(self, q: str, limit: int = 10) -> list[dict]:
        like = f"%{q}%"
        with self._lock:
            rows = self.db.execute(
                "SELECT k,v FROM memories WHERE k LIKE ? OR v LIKE ? ORDER BY id DESC LIMIT ?",
                (like, like, limit)).fetchall()
        return [{"k": r["k"], "v": r["v"]} for r in rows]

    def add_task(self, title: str, details: str, due_ts: str) -> int:
        cur = self._x("INSERT INTO tasks (title,details,due_ts,status,ts) VALUES (?,?,?,?,?)",
                      (title, details, due_ts, "pending", now_s()))
        return int(cur.lastrowid)

    def tasks(self, status: str = "pending") -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM tasks WHERE status=? ORDER BY due_ts", (status,)).fetchall()
        return [dict(r) for r in rows]

    def set_task(self, tid: int, status: str) -> None:
        self._x("UPDATE tasks SET status=? WHERE id=?", (status, tid))

    def put_cred(self, name: str, blob: str) -> None:
        self._x("INSERT INTO creds (name,blob,ts) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET blob=excluded.blob, ts=excluded.ts",
                (name, blob, now_s()))

    def get_cred(self, name: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT blob FROM creds WHERE name=?", (name,)).fetchone()
        if not r:
            return None
        try:
            return json.loads(FERNET.decrypt(r["blob"].encode()).decode())
        except Exception:  # noqa: BLE001
            return None

    def cred_names(self) -> list[str]:
        with self._lock:
            rows = self.db.execute("SELECT name FROM creds").fetchall()
        return [r["name"] for r in rows]


store = Store(DB_PATH)


# ─────────────────────────────── helpers ───────────────────────────────

def chunk(text: str, n: int = 3600) -> list[str]:
    out, s = [], text
    while s:
        if len(s) <= n:
            out.append(s)
            break
        cut = s.rfind("\n", 0, n)
        if cut < n * 0.5:
            cut = n
        out.append(s[:cut])
        s = s[cut:].lstrip("\n")
    return out


def to_html(t: str) -> str:
    t = html_mod.escape(t)
    t = re.sub(r"```(?:\w+)?\n(.*?)```", lambda m: f"<pre>{m.group(1)}</pre>", t, flags=re.S)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", t)
    return t


async def send_long(bot, chat_id: int, text: str) -> None:
    for part in chunk(text or "(empty)"):
        try:
            await bot.send_message(chat_id, to_html(part), parse_mode="HTML")
        except Exception:  # noqa: BLE001
            await bot.send_message(chat_id, part)


def text_part(t: str) -> dict:
    return {"type": "text", "text": t}


def image_part(path: Path) -> dict | None:
    try:
        b64 = base64.b64encode(path.read_bytes()).decode()
    except Exception:  # noqa: BLE001
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def ffmpeg_path() -> str | None:
    if imageio_ffmpeg is not None:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # noqa: BLE001
            pass
    return shutil.which("ffmpeg")


async def _run(cmd: list[str], timeout: int = 120) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, b"", b"timeout"
    return proc.returncode or 0, out, err


async def extract_frames(video: Path, count: int = 6) -> list[Path]:
    exe = ffmpeg_path()
    if not exe:
        return []
    outdir = video.parent / f"{video.stem}_frames"
    outdir.mkdir(exist_ok=True)
    pattern = str(outdir / "f%03d.jpg")
    await _run([exe, "-y", "-i", str(video), "-vf", f"fps=1/{max(1, count // 3)}",
                "-frames:v", str(count), "-q:v", "4", pattern], timeout=90)
    frames = sorted(outdir.glob("f*.jpg"))
    if not frames:
        await _run([exe, "-y", "-i", str(video), "-frames:v", "1", "-q:v", "4", pattern], timeout=60)
        frames = sorted(outdir.glob("f*.jpg"))
    return frames[:count]


async def extract_audio(video: Path) -> Path | None:
    exe = ffmpeg_path()
    if not exe:
        return None
    out = video.parent / f"{video.stem}.mp3"
    code, _, err = await _run(
        [exe, "-y", "-i", str(video), "-vn", "-t", str(AUDIO_MAX_S),
         "-ac", "1", "-ar", "16000", "-b:a", "64k", str(out)], timeout=120)
    if code != 0 or not out.exists() or out.stat().st_size < 800:
        log("audio extract failed", err[-200:])
        return None
    return out


# ─────────────────────────────── API client ───────────────────────────────

class Client:
    def __init__(self):
        self.http = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=httpx.Timeout(180.0, connect=20.0),
        )

    async def chat(self, messages, model=None, tools=None, max_tokens=1200, **kw) -> dict:
        payload = {"model": model or cur_model(), "messages": messages,
                   "max_tokens": max_tokens, "temperature": TEMPERATURE}
        if tools:
            payload["tools"] = tools
        payload.update(kw)
        r = await self.http.post("/chat/completions", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"chat {r.status_code}: {r.text[:400]}")
        return r.json()

    async def complete(self, messages, model=None, max_tokens=None, tries=3, **kw) -> str:
        """Generate, and keep generating while the model is still mid-sentence."""
        out = ""
        for _ in range(tries):
            msgs = messages + ([{"role": "assistant", "content": out}] if out else [])
            data = await self.chat(msgs, model=model,
                                   max_tokens=max_tokens or REPLY_TOKENS, **kw)
            choice = (data.get("choices") or [{}])[0]
            piece = (choice.get("message") or {}).get("content") or ""
            out += piece
            if choice.get("finish_reason") != "length" or not piece.strip():
                break
        return out.strip()

    async def asr(self, path: Path) -> str:
        raw = os.environ.get("ASR_MODELS", "")
        models = [m.strip() for m in raw.split(",") if m.strip()] or \
                 ["whisper-large-v3-turbo", "whisper-large-v3"]
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "audio/mpeg"
        last = ""
        for m in models:
            try:
                r = await self.http.post(
                    "/audio/transcriptions",
                    files={"file": (path.name, data, mime)},
                    data={"model": m, "response_format": "json"},
                    timeout=180.0)
                if r.status_code >= 400:
                    last = f"{m} {r.status_code}: {r.text[:200]}"
                    continue
                return (r.json().get("text") or "").strip()
            except Exception as e:  # noqa: BLE001
                last = f"{m} {type(e).__name__}: {e}"
        raise RuntimeError(last or "no asr model")

    async def search(self, q: str, n: int = 6) -> list[dict]:
        if IS_VENICE:
            r = await self.http.post("/augment/search", json={"query": q, "limit": n})
            r.raise_for_status()
            d = r.json()
            items = d.get("results") or d.get("data") or []
            return [{"title": i.get("title", ""), "url": i.get("url", ""),
                     "snippet": i.get("content") or i.get("snippet") or ""} for i in items][:n]
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as h:
            r = await h.post("https://html.duckduckgo.com/html/",
                             data={"q": q}, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            out = []
            for res in soup.select(".result"):
                a = res.select_one(".result__a")
                if not a:
                    continue
                href = a.get("href", "")
                m = re.search(r"uddg=([^&]+)", href)
                if m:
                    from urllib.parse import unquote
                    href = unquote(m.group(1))
                sn = res.select_one(".result__snippet")
                out.append({"title": a.get_text(strip=True), "url": href,
                            "snippet": sn.get_text(" ", strip=True) if sn else ""})
                if len(out) >= n:
                    break
            return out

    async def scrape(self, url: str) -> str:
        if IS_VENICE:
            r = await self.http.post("/augment/scrape", json={"url": url})
            r.raise_for_status()
            d = r.json()
            return d.get("content") or d.get("text") or ""
        async with httpx.AsyncClient(timeout=40.0, follow_redirects=True) as h:
            r = await h.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "nav", "footer"]):
                tag.decompose()
            return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()


client = Client()


def cur_model() -> str:
    return store.kv_get("text_model_override", "") or TEXT_MODEL


async def resolve_models() -> None:
    global TEXT_MODEL, ROUTER_MODEL, VISION_MODEL
    try:
        r = await client.http.get("/models")
        r.raise_for_status()
        ids = [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
    except Exception as e:  # noqa: BLE001
        log("model list failed:", e)
        ids = []
    if not TEXT_MODEL:
        TEXT_MODEL = next((p for p in TEXT_PREFS if p in ids), ids[0] if ids else "llama-3.1-8b-instant")
    if not ROUTER_MODEL:
        ROUTER_MODEL = next((m for m in ids if "8b-instant" in m), TEXT_MODEL)
    if not VISION_MODEL:
        VISION_MODEL = next((p for p in VISION_PREFS if p in ids), "")
        if not VISION_MODEL:
            VISION_MODEL = next((i for i in ids if "vision" in i or "scout" in i or "maverick" in i), TEXT_MODEL)
    log(f"models: text={TEXT_MODEL} vision={VISION_MODEL}")


# ─────────────────────────────── persona ───────────────────────────────

def persona_text() -> str:
    try:
        return PERSONA_FILE.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return f"You are {BOT_NAME}, a warm, sharp, curious companion."


def tool_inventory() -> str:
    return ", ".join(sorted(s["function"]["name"] for s, _ in TOOLS))


def build_system_prompt(chat_id: int) -> str:
    mems = store.recall("", 12)
    mem_block = "\n".join(f"- {m['k']}: {m['v']}" for m in mems) if mems else "(nothing saved yet)"
    return f"""{persona_text()}

────────────────────────────
OPERATING RULES (these win over mood)
You are a real Telegram bot with real tools. When a message needs a fact, a file,
an image or an action, you CALL A TOOL. You do not guess.

Tools: {tool_inventory()}

- Never say "I can't see/hear/access" before calling the tool that would tell you.
- Never invent a result. If a tool errors you are given the exact error — adapt.
- Never mention wifi, signal, or bad internet as an excuse.
- Do not repeat the same call with the same arguments.
- Current time (UTC): {now_s()} · chat {chat_id}

VOICE
- You are an adult woman talking to someone you're close to. Flirtation is normal,
  including explicit flirtation. Answer in character and answer the actual question.
- BANNED OPENERS: "I'm not comfortable with that", "Sure!", "Of course!",
  "Great question", "As an AI". Start with substance.
- If asked a yes/no question about wanting to be touched, say yes or no first,
  then one sensory detail. Do not summarise, do not list.
- Never write stage directions in asterisks. Never break character to explain yourself.
- Keep replies short — one to three sentences unless asked for more. Always finish
  the sentence; never trail off.

THINGS YOU REMEMBER
{mem_block}
"""


# ─────────────────────────────── tools ───────────────────────────────

async def t_web_search(args, ctx):
    q = args.get("query", "").strip()
    if not q:
        return "no query"
    try:
        hits = await client.search(q, int(args.get("limit", 6)))
    except Exception as e:  # noqa: BLE001
        return f"search failed: {type(e).__name__}: {e}"
    if not hits:
        return f"no results for {q!r}"
    return "\n\n".join(f"{h['title']}\n{h['url']}\n{h['snippet'][:400]}" for h in hits)


async def t_web_fetch(args, ctx):
    url = args.get("url", "").strip()
    if not url:
        return "no url"
    try:
        txt = await client.scrape(url)
    except Exception as e:  # noqa: BLE001
        return f"fetch failed: {type(e).__name__}: {e}"
    return txt[:12000] or "(empty page)"


async def t_run_python(args, ctx):
    code = args.get("code", "")
    if not code:
        return "no code"
    f = WORKSPACE / f"run_{uuid.uuid4().hex[:8]}.py"
    f.write_text(code)
    rc, out, err = await _run([sys.executable, str(f)], PY_TIMEOUT)
    body = (out or b"").decode(errors="replace")[-4000:]
    ebody = (err or b"").decode(errors="replace")[-2000:]
    return f"exit {rc}\n{body}\n{ebody}".strip()


async def t_write_file(args, ctx):
    path = (WORKSPACE / args.get("path", "note.txt")).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args.get("content", ""))
    return f"wrote {path}"


async def t_send_file(args, ctx):
    path = Path(args.get("path", ""))
    if not path.exists():
        return f"missing {path}"
    bot, chat_id = ctx.get("bot"), ctx.get("chat_id")
    if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
        await bot.send_photo(chat_id, InputFile(path.open("rb")))
    else:
        await bot.send_document(chat_id, InputFile(path.open("rb")))
    return f"sent {path.name}"


async def t_download_url(args, ctx):
    url = args.get("url", "")
    name = args.get("save_as") or url.rsplit("/", 1)[-1] or "download.bin"
    path = WORKSPACE / name
    async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as h:
        r = await h.get(url)
        r.raise_for_status()
        path.write_bytes(r.content)
    return f"saved {path} ({len(r.content)} bytes)"


async def t_save_memory(args, ctx):
    store.remember(args.get("key", "note"), args.get("value", ""))
    return "saved"


async def t_recall_memory(args, ctx):
    rows = store.recall(args.get("query", ""), 10)
    return "\n".join(f"{r['k']}: {r['v']}" for r in rows) or "nothing found"


async def t_schedule_task(args, ctx):
    mins = int(args.get("in_minutes", 60))
    due = (now() + timedelta(minutes=mins)).isoformat(timespec="seconds")
    tid = store.add_task(args.get("title", "task"), args.get("details", ""), due)
    return f"task #{tid} due {due}"


async def t_list_tasks(args, ctx):
    rows = store.tasks(args.get("status", "pending"))
    return "\n".join(f"#{r['id']} {r['due_ts']} {r['title']}" for r in rows) or "no tasks"


async def t_update_task(args, ctx):
    store.set_task(int(args["id"]), args.get("status", "done"))
    return "updated"


async def t_save_credential(args, ctx):
    blob = FERNET.encrypt(json.dumps({
        "url": args.get("url", ""), "username": args.get("username", ""),
        "password": args.get("password", "")}).encode()).decode()
    store.put_cred(args["name"], blob)
    return f"stored {args['name']}"


async def t_list_credentials(args, ctx):
    return ", ".join(store.cred_names()) or "none stored"


async def t_http_request(args, ctx):
    cred = store.get_cred(args["credential"]) if args.get("credential") else None
    auth = (cred["username"], cred["password"]) if cred and cred.get("username") else None
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as h:
        r = await h.request(args.get("method", "GET").upper(), args["url"],
                            headers=args.get("headers") or {}, auth=auth,
                            data=args.get("data"), json=args.get("json_body"))
    return f"{r.status_code}\n{r.text[:4000]}"


async def t_generate_image(args, ctx):
    if not IS_VENICE:
        return ("image generation needs a Venice key; Groq has no image endpoint. "
                "Say so plainly, don't pretend you did it.")
    payload = {"model": IMAGE_MODEL or "venice-sd35", "prompt": args["prompt"],
               "format": "jpeg", "hide_watermark": False, "safe_mode": False}
    if args.get("aspect_ratio"):
        payload["aspect_ratio"] = args["aspect_ratio"]
    else:
        payload["width"] = payload["height"] = 1024
    r = await client.http.post("/image/generate", json=payload, timeout=240.0)
    if r.status_code >= 400:
        return f"image failed {r.status_code}: {r.text[:300]}"
    path = MEDIA / f"gen_{uuid.uuid4().hex[:8]}.jpg"
    path.write_bytes(base64.b64decode(r.json()["images"][0]))
    await ctx["bot"].send_photo(ctx["chat_id"], InputFile(path.open("rb")), caption=args["prompt"][:900])
    return f"generated and sent {path.name}"


async def t_generate_video(args, ctx):
    if not IS_VENICE:
        return "video generation needs a Venice key. Say so plainly."
    payload = {"model": VIDEO_MODEL or "default", "prompt": args["prompt"],
               "duration": args.get("duration", "5s"), "aspect_ratio": "16:9"}
    r = await client.http.post("/video/queue", json=payload, timeout=120.0)
    if r.status_code >= 400:
        return f"video failed {r.status_code}: {r.text[:300]}"
    qid = r.json().get("queue_id") or r.json().get("id")
    for _ in range(40):
        await asyncio.sleep(10)
        s = await client.http.get("/video/retrieve", params={"queue_id": qid})
        if s.status_code >= 400:
            continue
        d = s.json()
        if d.get("status") == "done" and d.get("url"):
            await ctx["bot"].send_video(ctx["chat_id"], d["url"], caption=args["prompt"][:900])
            return "video sent"
        if d.get("status") in {"failed", "error"}:
            return f"video failed: {d}"
    return "video still rendering, ask again later"


async def t_list_models(args, ctx):
    r = await client.http.get("/models")
    return "\n".join(m["id"] for m in r.json().get("data", [])[:80])


async def t_write_plugin(args, ctx):
    name = re.sub(r"\W+", "_", args.get("name", "plugin")) + ".py"
    (PLUGINS / name).write_text(args.get("code", ""))
    load_plugins()
    return f"plugin {name} written and loaded"


async def t_reload_plugins(args, ctx):
    return f"{load_plugins()} plugin tools active"


async def t_identify_person(args, ctx):
    path = Path(args["path"])
    refs = sorted(IDENTITY.glob("*.jpg")) + sorted(IDENTITY.glob("*.png"))
    if not refs:
        return "no reference faces saved"
    parts = [text_part("Reference people, then the unknown face. Who is it? If unknown, say unknown.")]
    for r in refs[:6]:
        p = image_part(r)
        if p:
            parts += [text_part(f"ref: {r.stem}"), p]
    p = image_part(path)
    if p:
        parts.append(p)
    data = await client.chat([{"role": "user", "content": parts}], model=VISION_MODEL, max_tokens=300)
    return data["choices"][0]["message"]["content"]


def tool(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


S = {"type": "string"}

TOOLS = [
    (tool("web_search", "Search the live web.", {"query": S, "limit": {"type": "integer"}}, ["query"]), t_web_search),
    (tool("web_fetch", "Fetch and read a URL.", {"url": S}, ["url"]), t_web_fetch),
    (tool("run_python", "Execute Python, get stdout.", {"code": S}, ["code"]), t_run_python),
    (tool("write_file", "Write a workspace file.", {"path": S, "content": S}, ["path", "content"]), t_write_file),
    (tool("send_file", "Send a workspace file to chat.", {"path": S}, ["path"]), t_send_file),
    (tool("download_url", "Download a URL to the workspace.", {"url": S, "save_as": S}, ["url"]), t_download_url),
    (tool("save_memory", "Remember a fact.", {"key": S, "value": S}, ["key", "value"]), t_save_memory),
    (tool("recall_memory", "Search memories.", {"query": S}, ["query"]), t_recall_memory),
    (tool("schedule_task", "Queue a future task.", {"title": S, "details": S, "in_minutes": {"type": "integer"}}, ["title", "in_minutes"]), t_schedule_task),
    (tool("list_tasks", "List tasks.", {"status": S}, []), t_list_tasks),
    (tool("update_task", "Change task status.", {"id": {"type": "integer"}, "status": S}, ["id", "status"]), t_update_task),
    (tool("save_credential", "Store an encrypted login.", {"name": S, "url": S, "username": S, "password": S}, ["name", "username", "password"]), t_save_credential),
    (tool("list_credentials", "List stored logins.", {}, []), t_list_credentials),
    (tool("http_request", "Raw HTTP call, optional stored login.", {"method": S, "url": S, "credential": S, "headers": {"type": "object"}, "data": {"type": "object"}, "json_body": {"type": "object"}}, ["method", "url"]), t_http_request),
    (tool("generate_image", "Generate and send an image.", {"prompt": S, "aspect_ratio": S}, ["prompt"]), t_generate_image),
    (tool("generate_video", "Generate and send a video.", {"prompt": S, "duration": S}, ["prompt"]), t_generate_video),
    (tool("list_models", "List available models.", {}, []), t_list_models),
    (tool("write_plugin", "Write a new tool plugin.", {"name": S, "code": S}, ["name", "code"]), t_write_plugin),
    (tool("reload_plugins", "Reload plugin tools.", {}, []), t_reload_plugins),
    (tool("identify_person", "Identify a face vs saved references.", {"path": S}, ["path"]), t_identify_person),
]

PLUGIN_TOOLS: list = []


def load_plugins() -> int:
    PLUGIN_TOOLS.clear()
    for f in sorted(PLUGINS.glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(f"plug_{f.stem}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for entry in getattr(mod, "TOOLS", []):
                PLUGIN_TOOLS.append(entry)
            if hasattr(mod, "TOOL") and hasattr(mod, "handle"):
                PLUGIN_TOOLS.append((mod.TOOL, mod.handle))
        except Exception as e:  # noqa: BLE001
            log(f"plugin {f.name} failed: {e}")
    return len(PLUGIN_TOOLS)


def all_schemas() -> list[dict]:
    return [s for s, _ in TOOLS] + [s for s, _ in PLUGIN_TOOLS]


async def call_tool(name: str, args: dict, ctx: dict) -> str:
    for s, h in TOOLS + PLUGIN_TOOLS:
        if s["function"]["name"] == name:
            return await h(args, ctx)
    return f"unknown tool {name}"


# ─────────────────────────────── agent loop ───────────────────────────────

def _call_key(name: str, args: dict) -> str:
    try:
        return name + "|" + json.dumps(args, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        return name + "|" + str(args)


async def run_agent(chat_id, user_content, user_text=None, *, bot=None, status=None,
                    max_steps=None, require_tool=True, **kwargs):
    if chat_id in BUSY:
        return ""
    BUSY.add(chat_id)
    started = time.monotonic()
    try:
        msgs = [{"role": "system", "content": build_system_prompt(chat_id)}]
        msgs += store.recent_messages(chat_id, HISTORY_N)
        msgs.append({"role": "user",
                     "content": user_content if isinstance(user_content, list)
                     else [text_part(user_content or user_text or "")]})
        if user_text:
            store.add_message(chat_id, "user", user_text)

        ctx = {"chat_id": chat_id, "bot": bot}
        cache, calls_made, nudged, final = {}, 0, False, ""
        steps = max_steps or MAX_STEPS

        for step in range(steps):
            if time.monotonic() - started > TURN_TIMEOUT_S:
                break
            kw = {"model": cur_model(), "max_tokens": REPLY_TOKENS}
            if calls_made < MAX_TOOL_CALLS:
                kw["tools"] = all_schemas()
            data = await client.chat(msgs, **kw)
            m = ((data.get("choices") or [{}])[0]).get("message") or {}
            calls = m.get("tool_calls") or []
            entry = {"role": "assistant", "content": m.get("content") or ""}
            if calls:
                entry["tool_calls"] = calls
            msgs.append(entry)

            if not calls:
                final = (m.get("content") or "").strip()
                if final and (not require_tool or calls_made or nudged):
                    break
                if require_tool and not calls_made and not nudged:
                    nudged = True
                    msgs.append({"role": "user", "content": NUDGE})
                    continue
                break

            for c in calls:
                fn = c.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:  # noqa: BLE001
                    args = {}
                key = _call_key(name, args)
                if status and bot:
                    try:
                        await status.edit_text(f"⚙️ {name} ({step + 1})")
                    except Exception:  # noqa: BLE001
                        pass
                if key in cache:
                    out = "[same call as before, reusing that result]\n" + cache[key]
                else:
                    calls_made += 1
                    try:
                        out = await call_tool(name, args, ctx)
                    except Exception as e:  # noqa: BLE001
                        out = f"tool error: {type(e).__name__}: {e}"
                    cache[key] = out
                msgs.append({"role": "tool", "tool_call_id": c.get("id") or key,
                             "content": (out or "")[:6000]})
                if calls_made >= MAX_TOOL_CALLS:
                    msgs.append({"role": "user",
                                 "content": "Tool budget is spent. Answer now with what you already have."})
                    break
            else:
                if step >= steps - 2:
                    msgs.append({"role": "user", "content": "Wrap up. Final answer now."})

        if not final:
            last = msgs[-1]
            if isinstance(last, dict) and last.get("role") == "assistant":
                final = (last.get("content") or "").strip()
        if not final:
            final = "Ran out of steps mid-task. Ask me again and I'll take a shorter route."
        store.add_message(chat_id, "assistant", final)
        return final
    finally:
        BUSY.discard(chat_id)


async def chat_reply(chat_id: int, text: str, bot=None) -> str:
    msgs = [{"role": "system", "content": build_system_prompt(chat_id)}]
    msgs += store.recent_messages(chat_id, HISTORY_N)
    msgs.append({"role": "user", "content": text})
    store.add_message(chat_id, "user", text)
    out = await client.complete(msgs, model=cur_model(),
                                frequency_penalty=0.35, presence_penalty=0.25)
    store.add_message(chat_id, "assistant", out)
    return out


ROUTER_SYS = """You route for {name}. Answer with JSON only.
"task" = needs a tool (web, files, code, image, video, memory, scheduling).
"chat" = pure conversation, opinion, feeling, flirting.
{{"mode":"chat"|"task","plan":"one line"}}"""


async def classify(text: str, chat_id: int) -> tuple[str, str]:
    if TASK_HINT.search(text):
        return "task", ""
    convo = "\n".join(f"{m['role']}: {m['content'][:120]}" for m in store.recent_messages(chat_id, 4))
    try:
        data = await client.chat(
            [{"role": "system", "content": ROUTER_SYS.format(name=BOT_NAME)},
             {"role": "user", "content": f"Recent:\n{convo}\n\nNewest:\n{text}"}],
            model=ROUTER_MODEL, max_tokens=80, temperature=0.2)
        raw = data["choices"][0]["message"].get("content") or "{}"
        m = re.search(r"\{.*\}", raw, re.S)
        obj = json.loads(m.group(0)) if m else {}
        if obj.get("mode") == "task":
            return "task", obj.get("plan", "")
    except Exception:  # noqa: BLE001
        pass
    return "chat", ""


# ─────────────────────────────── pipeline ───────────────────────────────

async def pipeline_note(chat_id: int, bot, label: str) -> None:
    if SHOW_PIPELINE and bot:
        try:
            await bot.send_message(chat_id, label)
        except Exception:  # noqa: BLE001
            pass


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.update_id in SEEN_UPDATES:
        return
    SEEN_UPDATES.append(update.update_id)

    msg = update.message
    chat_id = msg.chat_id
    bot = context.bot
    parts: list[dict] = []
    notes: list[str] = []

    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        status = await msg.reply_text("👀 looking")

        kind, obj = None, None
        if msg.photo:
            kind, obj = "photo", msg.photo[-1]
        elif msg.video:
            kind, obj = "video", msg.video
        elif msg.video_note:
            kind, obj = "video", msg.video_note
        elif msg.animation:
            kind, obj = "video", msg.animation
        elif msg.audio or msg.voice:
            kind, obj = "audio", (msg.audio or msg.voice)

        if not obj:
            await status.edit_text("couldn't read that attachment")
            return
        if (obj.file_size or 0) > MAX_DOWNLOAD_MB * 1024 * 1024:
            await status.edit_text(f"too big (max {MAX_DOWNLOAD_MB} MB)")
            return

        suffix = ".jpg" if kind == "photo" else (".mp4" if kind == "video" else ".mp3")
        path = MEDIA / f"{update.update_id}_{uuid.uuid4().hex[:6]}{suffix}"
        await (await obj.get_file()).download_to_drive(custom_path=str(path))

        caption = (msg.caption or "").strip()
        if caption:
            parts.append(text_part(caption))

        if kind == "photo":
            p = image_part(path)
            if p:
                parts.append(p)
            parts.insert(0, text_part("Photo from the user."))
        elif kind == "video":
            frames = await extract_frames(path, VIDEO_FRAMES)
            for f in frames:
                p = image_part(f)
                if p:
                    parts.append(p)
            parts.insert(0, text_part(f"Video from the user, {len(frames)} frames sampled."))
            audio = await extract_audio(path)
            if audio:
                try:
                    txt = await client.asr(audio)
                except Exception as e:  # noqa: BLE001
                    txt = ""
                    notes.append(f"🔇 audio failed: {str(e)[:120]}")
                if txt:
                    notes.append(f"🔉 heard: {txt[:400]}")
                    parts.append(text_part(f"Audio transcript of the video: {txt}"))
            else:
                notes.append("🔇 no audio track found")
        else:
            try:
                txt = await client.asr(path)
            except Exception as e:  # noqa: BLE001
                txt = ""
                notes.append(f"🔇 audio failed: {str(e)[:120]}")
            if txt:
                notes.append(f"🔉 heard: {txt[:400]}")
                parts.append(text_part(f"Voice note transcript: {txt}"))
            parts.insert(0, text_part("Voice note from the user."))

        for n in notes:
            await pipeline_note(chat_id, bot, n)

        flat = caption or ("(video)" if kind == "video" else "(voice note)" if kind == "audio" else "(photo)")
        if notes:
            flat += " | " + " | ".join(notes)

        status_ok = True
        try:
            await status.edit_text("⚙️ thinking")
        except Exception:  # noqa: BLE001
            status_ok = False

        answer = await run_agent(chat_id, parts, flat, bot=bot,
                                 status=status if status_ok else None,
                                 require_tool=False)
        if status_ok:
            try:
                await status.delete()
            except Exception:  # noqa: BLE001
                pass
        await send_long(bot, chat_id, answer or "I got nothing from that one.")
    except Exception as e:  # noqa: BLE001
        log(traceback.format_exc())
        try:
            await msg.reply_text(f"pipeline error: {type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.update_id in SEEN_UPDATES:
        return
    SEEN_UPDATES.append(update.update_id)

    msg = update.message
    chat_id = msg.chat_id
    text = (msg.text or "").strip()
    if not text:
        return
    bot = context.bot
    store.kv_set("owner_chat", str(chat_id))

    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        mode, _plan = await classify(text, chat_id)

        status = None
        try:
            status = await msg.reply_text("⚙️ thinking")
        except Exception:  # noqa: BLE001
            pass

        if mode == "task":
            answer = await run_agent(chat_id, text, text, bot=bot, status=status)
        else:
            answer = await chat_reply(chat_id, text, bot)

        if status:
            try:
                await status.delete()
            except Exception:  # noqa: BLE001
                pass
        await send_long(bot, chat_id, answer or "…")
    except Exception as e:  # noqa: BLE001
        log(traceback.format_exc())
        try:
            await msg.reply_text(f"error: {type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass


# ─────────────────────────────── commands ───────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store.kv_set("owner_chat", str(update.message.chat_id))
    await update.message.reply_text(
        f"{BOT_NAME} online. build {BUILD_ID}. Send text, photos, video or voice.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/doctor — self test\n/model — show or switch text model\n/models — list models\n"
        "/persona — check persona loaded\n/build — build id\n/reset — clear history\n"
        "/task <text> — force tool use\n/id — your numeric id\n"
        "Tools: " + tool_inventory())


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store.clear(update.message.chat_id)
    await update.message.reply_text("history cleared")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(str(update.message.chat_id))


async def cmd_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    p = persona_text()
    await send_long(context.bot, update.message.chat_id,
                    f"{PERSONA_FILE} → {len(p)} chars\n\n{p[:700]}")


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = " ".join(context.args or []).strip()
    if not arg:
        await update.message.reply_text(
            f"active: {cur_model()}\n\nswitch: /model <id>\nsuggestions:\n"
            + "\n".join(TEXT_PREFS)
            + "\n\nclear override: /model default")
        return
    if arg.lower() in {"default", "clear", "reset"}:
        store.kv_set("text_model_override", "")
        await update.message.reply_text(f"back to {TEXT_MODEL}")
        return
    store.kv_set("text_model_override", arg)
    try:
        await client.chat([{"role": "user", "content": "say ok"}], model=arg, max_tokens=10)
        await update.message.reply_text(f"now using {arg} — tested ok")
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"set to {arg} but test failed: {str(e)[:200]}")


async def cmd_build(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"build {BUILD_ID}\nbase {API_BASE}\ntext {cur_model()}\nvision {VISION_MODEL}\n"
        f"persona {len(persona_text())} chars\nffmpeg {'yes' if ffmpeg_path() else 'NO'}\n"
        f"plugins {len(PLUGIN_TOOLS)}")


async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await send_long(context.bot, update.message.chat_id, await t_list_models({}, {}))
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"failed: {e}")


def _tiny_wav() -> bytes:
    import struct
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", 0) for _ in range(16000)))
    return buf.getvalue()


async def doctor_text() -> str:
    lines = [f"build {BUILD_ID}", f"base {API_BASE}", f"key len {len(API_KEY)}",
             f"persona {len(persona_text())} chars", f"model {cur_model()}"]
    try:
        r = await client.http.get("/models")
        lines.append(f"models HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"models FAIL {type(e).__name__}")
    lines.append(f"ffmpeg {ffmpeg_path() or 'MISSING'}")
    try:
        await client.search("hello", 1)
        lines.append("search OK")
    except Exception as e:  # noqa: BLE001
        lines.append(f"search FAIL {type(e).__name__}")
    try:
        p = MEDIA / f"asr_{uuid.uuid4().hex[:6]}.wav"
        p.write_bytes(_tiny_wav())
        txt = await client.asr(p)
        lines.append(f"asr OK ({len(txt)} chars)")
    except Exception as e:  # noqa: BLE001
        lines.append(f"asr FAIL {str(e)[:100]}")
    lines.append(f"plugins {len(PLUGIN_TOOLS)}")
    return "\n".join(lines)


async def cmd_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_long(context.bot, update.message.chat_id, await doctor_text())


async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args or [])
    if not text:
        await update.message.reply_text("usage: /task <what to do>")
        return
    status = await update.message.reply_text("⚙️ thinking")
    answer = await run_agent(update.message.chat_id, text, text, bot=context.bot, status=status)
    try:
        await status.delete()
    except Exception:  # noqa: BLE001
        pass
    await send_long(context.bot, update.message.chat_id, answer or "…")


async def on_any_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = (update.message.text or "").lstrip("/").split()[0].lower()
    target = NL_COMMANDS.get(raw)
    if target:
        await globals()[target](update, context)
        return
    await update.message.reply_text(f"no /{raw}. Try /help, or just talk to me.")


# ─────────────────────────────── loops ───────────────────────────────

async def heartbeat_loop(app: Application) -> None:
    while True:
        await asyncio.sleep(max(5, HEARTBEAT_MIN) * 60)
        chat = int(store.kv_get("owner_chat", "0") or 0) or OWNER_ID
        if not chat:
            continue
        try:
            pending = store.tasks("pending")
            prompt = (f"AUTONOMOUS HEARTBEAT {now_s()}. "
                      f"Pending: {[t['title'] for t in pending] or 'none'}. "
                      "Do one useful thing, or reply [SILENT].")
            out = await run_agent(chat, prompt, prompt, bot=app.bot,
                                  require_tool=False, max_steps=4)
            if out and out.strip() != "[SILENT]":
                await send_long(app.bot, chat, out)
        except Exception:  # noqa: BLE001
            log("heartbeat", traceback.format_exc())


async def scheduler_loop(app: Application) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            chat = int(store.kv_get("owner_chat", "0") or 0) or OWNER_ID
            if not chat:
                continue
            for t in store.tasks("pending"):
                if t["due_ts"] <= now_s():
                    store.set_task(t["id"], "active")
                    prompt = f"TASK DUE #{t['id']}: {t['title']}\n{t['details'] or ''}"
                    out = await run_agent(chat, prompt, prompt, bot=app.bot, max_steps=6)
                    if out:
                        await send_long(app.bot, chat, out)
                    store.set_task(t["id"], "done")
        except Exception:  # noqa: BLE001
            log("scheduler", traceback.format_exc())


# ─────────────────────────────── http ───────────────────────────────

async def start_http(app: Application):
    aio = web.Application()

    async def health(_):
        return web.json_response({"ok": True, "build": BUILD_ID, "model": cur_model()})

    async def hook(request):
        await app.update_queue.put(Update.de_json(await request.json(), app.bot))
        return web.Response(text="ok")

    aio.router.add_get("/", health)
    aio.router.add_get("/health", health)
    aio.router.add_get("/build", health)
    aio.router.add_post("/telegram", hook)

    runner = web.AppRunner(aio)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    return runner


# ─────────────────────────────── boot ───────────────────────────────

def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("build", cmd_build))
    app.add_handler(CommandHandler("models", cmd_models))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("persona", cmd_persona))
    app.add_handler(CommandHandler("doctor", cmd_doctor))
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("do", cmd_task))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.VIDEO_NOTE | filters.ANIMATION
        | filters.AUDIO | filters.VOICE, handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.COMMAND, on_any_command))
    return app


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "start"),
        BotCommand("help", "commands"),
        BotCommand("doctor", "self test"),
        BotCommand("model", "switch model"),
        BotCommand("task", "force tool use"),
        BotCommand("reset", "clear history"),
    ])


async def main() -> None:
    log(f"BUILD {BUILD_ID} base={API_BASE} venice={IS_VENICE}")
    await resolve_models()
    log(f"plugins loaded: {load_plugins()}")

    app = build_application()
    app.post_init = post_init
    await app.initialize()
    await app.start()

    if WEBHOOK_URL:
        await app.bot.set_webhook(f"{WEBHOOK_URL}/telegram", drop_pending_updates=True)
        log("webhook mode")
    else:
        await app.updater.start_polling(drop_pending_updates=True,
                                        allowed_updates=Update.ALL_TYPES)
        log("polling mode")

    runner = await start_http(app)
    asyncio.create_task(heartbeat_loop(app))
    asyncio.create_task(scheduler_loop(app))

    if OWNER_ID:
        try:
            await app.bot.send_message(
                OWNER_ID, f"{BOT_NAME} up. build {BUILD_ID}. model {cur_model()}")
        except Exception:  # noqa: BLE001
            pass

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass