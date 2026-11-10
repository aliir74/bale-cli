"""bale: a thin CLI over aiobale for Ali's own Bale user account.

Session lives in ~/.bale/session.bale (chmod 600). Targets are given as
`me` (Saved Messages), `g:<id>` (group), `u:<id>` (user), or a bare id that
is resolved from the dialog cache written by `bale chats list`.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import pathlib
import sys
import time

import click

HOME = pathlib.Path(os.environ.get("BALE_HOME", pathlib.Path.home() / ".bale"))
SESSION = HOME / "session.bale"
PENDING = HOME / "pending.json"
CACHE = HOME / "dialogs.json"


def _ensure_home() -> None:
    HOME.mkdir(mode=0o700, exist_ok=True)


def _patch_http_timeout() -> None:
    """aiobale's shared HTTP session gets aiohttp's default 5-minute total timeout,
    which kills uploads of large files (videos over ~40 MB). Drop the total cap."""
    import aiohttp
    from aiobale.client.session.aiohttp import AiohttpSession

    if getattr(AiohttpSession, "_bale_cli_patched", False):
        return
    orig = AiohttpSession._create_client_session

    def patched(self, timeout=None):
        return orig(self, timeout or aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=900))

    AiohttpSession._create_client_session = patched
    AiohttpSession._bale_cli_patched = True


def _client(require_auth: bool = True):
    from aiobale import Client

    _patch_http_timeout()

    _ensure_home()
    if require_auth and not SESSION.exists():
        raise click.ClickException("Not logged in. Run: bale auth send-code --phone <number>")
    c = Client(session_file=str(SESSION) if SESSION.exists() or not require_auth else None)
    return c


def _run(coro):
    return asyncio.run(coro)


def _out(data, as_json: bool, fmt=None):
    if as_json:
        click.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    elif fmt:
        for row in data if isinstance(data, list) else [data]:
            click.echo(fmt(row))
    else:
        click.echo(data)


def _ts(ms: int | None) -> str:
    if not ms:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


def _content_summary(content) -> str:
    if content is None:
        return ""
    t = getattr(content, "text", None)
    if t is not None and getattr(t, "value", None):
        return t.value
    bm = getattr(content, "bot_message", None)
    if bm is not None:
        for a in ("message", "text"):
            v = getattr(bm, a, None)
            v = getattr(v, "value", v)
            if isinstance(v, str) and v:
                return v
    doc = getattr(content, "document", None)
    if doc is not None:
        name = getattr(doc, "name", None) or "file"
        cap = getattr(getattr(doc, "caption", None), "text", None)
        return f"[{name}]" + (f" {cap}" if cap else "")
    d = content.model_dump(exclude_none=True) if hasattr(content, "model_dump") else {}
    return f"[{', '.join(d.keys()) or 'media'}]"


# ---------- target resolution ----------

def _load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {}


async def _resolve(client, target: str):
    from aiobale.enums import ChatType

    t = target.strip()
    if t == "me":
        me = client.me
        if me is None:
            raise click.ClickException("No user in session.")
        return me.id, ChatType.PRIVATE
    if ":" in t:
        kind, _, raw = t.partition(":")
        kinds = {"g": ChatType.GROUP, "group": ChatType.GROUP, "u": ChatType.PRIVATE,
                 "user": ChatType.PRIVATE, "c": ChatType.CHANNEL, "channel": ChatType.CHANNEL}
        if kind not in kinds:
            raise click.ClickException(f"Unknown target kind '{kind}'. Use g:, u:, c: or me.")
        return int(raw), kinds[kind]
    if t.lstrip("-").isdigit():
        entry = _load_cache().get(t)
        if not entry:
            raise click.ClickException(
                f"Id {t} not in dialog cache. Run `bale chats list` or use g:{t} / u:{t}.")
        return int(t), ChatType(entry["type"])
    raise click.ClickException(f"Bad target '{target}'.")


# ---------- dialogs ----------

class _D:  # minimal dialog record; aiobale's PeerData rejects some live dialogs
    def __init__(self, raw):
        from aiobale.types import Peer
        from aiobale.types import PeerData
        self.peer = Peer.model_validate(raw["1"])
        self.unread_count = raw.get("2", 0) if isinstance(raw.get("2"), int) else 0
        self.date = raw.get("6") if isinstance(raw.get("6"), int) else raw.get("3")
        self.content = None
        try:
            self.content = PeerData.model_validate(raw).content
        except Exception:
            pass


async def _raw_call(client, call):
    """Run an aiobale method but return the decoded protobuf dict, skipping pydantic models."""
    sess = client.session
    orig = sess.decode_result
    sess.decode_result = lambda result, method: result
    try:
        return await client(call)
    finally:
        sess.decode_result = orig


async def _raw_user_names(client, ids):
    from aiobale.methods import LoadUsers
    from aiobale.types import InfoPeer
    from aiobale.enums import ChatType

    names = {}
    for i in range(0, len(ids), 50):
        peers = [InfoPeer(id=x, type=ChatType.PRIVATE) for x in ids[i:i + 50]]
        raw = await _raw_call(client, LoadUsers(peers=peers))
        items = raw.get("1", [])
        for u in items if isinstance(items, list) else [items]:
            if isinstance(u, dict) and "1" in u:
                local, name = u.get("4"), u.get("3")
                local = local.get("1") if isinstance(local, dict) else local
                names[u["1"]] = local or name
    return names


async def _raw_dialogs(client, limit: int):
    """LoadDialogs without strict model validation: skips only the bad field, not the call."""
    from aiobale.methods import LoadDialogs

    sess = client.session
    orig = sess.decode_result
    sess.decode_result = lambda result, method: result
    try:
        raw = await client(LoadDialogs(offset_date=-1, limit=limit, exclude_pinned=False))
    finally:
        sess.decode_result = orig
    items = raw.get("3", [])
    if not isinstance(items, list):
        items = [items]
    out = []
    for it in items:
        try:
            out.append(_D(it))
        except Exception as e:
            click.echo(f"warn: skipped dialog: {e}", err=True)
    return out


async def _dialogs(client, limit: int):
    from aiobale.enums import ChatType
    from aiobale.types import Peer

    dialogs = await _raw_dialogs(client, limit)
    me_id = client.me.id if client.me else None
    user_peers = [d.peer for d in dialogs if int(d.peer.type) == int(ChatType.PRIVATE)]
    names: dict[int, str] = {}
    if user_peers:
        try:
            names = await _raw_user_names(client, [p.id for p in user_peers])
        except Exception as e:  # titles are best effort
            click.echo(f"warn: load_users failed: {e}", err=True)
    rows = []
    for d in dialogs:
        pid, ptype = d.peer.id, int(d.peer.type)
        title, members = names.get(pid), None
        if ptype in (int(ChatType.GROUP), int(ChatType.CHANNEL), int(ChatType.SUPER_GROUP)):
            try:
                g = await client.get_full_group(pid)
                title, members = g.title, g.members_count
            except Exception as e:
                title = f"<group {pid}: {e}>"
        if pid == me_id and ptype == int(ChatType.PRIVATE):
            title = "Saved Messages (me)"
        rows.append({
            "id": pid,
            "type": ptype,
            "type_name": ChatType(ptype).name.lower() if ptype in [x.value for x in ChatType] else str(ptype),
            "title": title or str(pid),
            "members": members,
            "unread": d.unread_count,
            "last_date": _ts(d.date),
            "last": " ".join(_content_summary(d.content).split())[:80],
        })
    _ensure_home()
    cache = _load_cache()
    for r in rows:
        cache[str(r["id"])] = {"type": r["type"], "title": r["title"]}
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    return rows


def _fmt_dialog(r):
    prefix = {1: "u", 2: "g", 3: "c", 5: "g"}.get(r["type"], "?")
    m = f" ({r['members']} members)" if r["members"] else ""
    return f"{prefix}:{r['id']}\t{r['title']}{m}\tunread={r['unread']}\t{r['last_date']}\t{r['last']}"


# ---------- CLI ----------

@click.group()
@click.version_option("0.3.0", prog_name="bale")
def cli():
    """Bale messenger CLI acting as your own user account (via aiobale)."""


@cli.group()
def auth():
    """Login, status, logout."""


@auth.command("send-code")
@click.option("--phone", required=True, help="Phone number, e.g. 98912xxxxxxx or 0912xxxxxxx.")
def auth_send_code(phone):
    """Step 1: ask Bale to send a login code."""
    from aiobale.enums import AuthErrors
    from aiobale.client.auth_cli import PhoneLoginCLI

    num = PhoneLoginCLI.normalize_phone_number(phone)

    async def go():
        c = _client(require_auth=False)
        try:
            resp = await c.start_phone_auth(num)
        finally:
            await c.session.close()
        if isinstance(resp, AuthErrors):
            raise click.ClickException(f"Bale refused: {resp.name}")
        PENDING.write_text(json.dumps({"phone": num, "hash": resp.transaction_hash, "at": time.time()}))
        os.chmod(PENDING, 0o600)
        click.echo(f"Code sent to {num}. Next: bale auth verify --code <code>")

    _run(go())


@auth.command("verify")
@click.option("--code", required=True)
@click.option("--password-env", default="BALE_2FA_PASSWORD", show_default=True,
              help="Env var holding the two-step password, if the account has one.")
def auth_verify(code, password_env):
    """Step 2: submit the code (and 2FA password from env if needed)."""
    from aiobale.enums import AuthErrors

    if not PENDING.exists():
        raise click.ClickException("No pending login. Run `bale auth send-code` first.")
    pending = json.loads(PENDING.read_text())

    async def go():
        from aiobale import Client
        c = Client(session_file=str(SESSION))
        try:
            res = await c.validate_code(code.strip(), pending["hash"])
            if res == AuthErrors.PASSWORD_NEEDED:
                pw = os.environ.get(password_env)
                if not pw:
                    raise click.ClickException(
                        f"Two-step password needed. Set {password_env} and rerun verify with the same code.")
                res = await c.validate_password(pw, pending["hash"])
            if isinstance(res, AuthErrors):
                raise click.ClickException(f"Login failed: {res.name}")
        finally:
            await c.session.close()
        os.chmod(SESSION, 0o600)
        PENDING.unlink(missing_ok=True)
        click.echo(f"Logged in. Session: {SESSION}")

    _run(go())


@auth.command("status")
@click.option("--json", "as_json", is_flag=True)
def auth_status(as_json):
    """Show the logged-in account (live call)."""
    async def go():
        c = _client()
        try:
            me = await c.get_me()
        finally:
            await c.session.close()
        u = me.model_dump(exclude_none=True, mode="json") if hasattr(me, "model_dump") else str(me)
        data = {"id": c.id, "user": u}
        _out(data, as_json, lambda d: f"Logged in as id={d['id']}")
    _run(go())


@auth.command("logout")
def auth_logout():
    """Sign out this device and delete the local session."""
    async def go():
        c = _client()
        await c.sign_out(delete_session=True)
        click.echo("Signed out.")
    _run(go())


@cli.command("whoami")
@click.pass_context
def whoami(ctx):
    """Alias of `auth status`."""
    ctx.invoke(auth_status, as_json=False)


@cli.group()
def chats():
    """List or search dialogs (also refreshes the id cache)."""


@chats.command("list")
@click.option("--limit", default=40, show_default=True)
@click.option("--groups", is_flag=True, help="Only groups/channels.")
@click.option("--json", "as_json", is_flag=True)
def chats_list(limit, groups, as_json):
    async def go():
        c = _client()
        try:
            rows = await _dialogs(c, limit)
        finally:
            await c.session.close()
        if groups:
            rows = [r for r in rows if r["type"] != 1]
        _out(rows, as_json, _fmt_dialog)
    _run(go())


@chats.command("search")
@click.argument("query")
@click.option("--limit", default=100, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def chats_search(query, limit, as_json):
    """Find dialogs whose title contains QUERY."""
    async def go():
        c = _client()
        try:
            rows = await _dialogs(c, limit)
        finally:
            await c.session.close()
        q = query.casefold()
        _out([r for r in rows if q in (r["title"] or "").casefold()], as_json, _fmt_dialog)
    _run(go())


@chats.command("read")
@click.option("--chat", "target", required=True, help="me | g:<id> | u:<id> | <id>")
def chats_read(target):
    """Mark a chat as read (seen)."""
    async def go():
        c = _client()
        try:
            cid, ctype = await _resolve(c, target)
            await c.seen_chat(cid, ctype)
        finally:
            await c.session.close()
        click.echo(f"Marked {target} as read.")
    _run(go())


@cli.group()
def messages():
    """Read message history."""


@messages.command("list")
@click.option("--chat", "target", required=True, help="me | g:<id> | u:<id> | <id>")
@click.option("--limit", default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def messages_list(target, limit, as_json):
    async def go():
        c = _client()
        try:
            cid, ctype = await _resolve(c, target)
            msgs = await c.load_history(cid, ctype, limit=limit)
        finally:
            await c.session.close()
        rows = [{"id": m.message_id, "date": _ts(m.date), "sender_id": m.sender_id,
                 "text": _content_summary(m.content)} for m in msgs]
        rows.sort(key=lambda r: r["date"])
        _out(rows, as_json, lambda r: f"{r['date']}\t{r['sender_id']}\t#{r['id']}\t{r['text']}")
    _run(go())


@cli.group()
def send():
    """Send text or media. Gated by the bale permission hook."""


def _sent(msg, as_json):
    data = {"ok": True, "message_id": getattr(msg, "message_id", None),
            "chat_id": getattr(getattr(msg, "chat", None), "id", None)}
    _out(data, as_json, lambda d: f"sent message_id={d['message_id']} chat={d['chat_id']}")


@send.command("text")
@click.option("--to", "target", required=True, help="me | g:<id> | u:<id> | <id>")
@click.option("--json", "as_json", is_flag=True)
@click.argument("text")
def send_text(target, as_json, text):
    async def go():
        c = _client()
        try:
            cid, ctype = await _resolve(c, target)
            msg = await c.send_message(text, cid, ctype)
        finally:
            await c.session.close()
        _sent(msg, as_json)
    _run(go())


def _thumb_and_size(path: pathlib.Path):
    """Return (thumb_bytes|None, w, h, duration_ms|None) for images/GIFs via Pillow."""
    try:
        from PIL import Image
    except ImportError:
        return None, 1000, 1000, None
    try:
        with Image.open(path) as img:
            w, h = img.size
            dur = None
            if getattr(img, "is_animated", False):
                total = 0
                for i in range(img.n_frames):
                    img.seek(i)
                    total += img.info.get("duration", 100) or 100
                dur = total
                img.seek(0)
            frame = img.convert("RGB")
            for side in (60, 40, 24):
                t = frame.copy()
                t.thumbnail((side, side))
                buf = io.BytesIO()
                t.save(buf, format="JPEG", quality=40)
                if buf.tell() <= 2048:
                    return buf.getvalue(), w, h, dur
            return None, w, h, dur
    except Exception:
        return None, 1000, 1000, None


def _video_meta(path: pathlib.Path):
    """Return (thumb_bytes|None, w, h, duration_ms|None) for a video via ffprobe/ffmpeg."""
    import json as _json, shutil, subprocess, tempfile
    w, h, dur = 1000, 1000, None
    if shutil.which("ffprobe"):
        try:
            r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=width,height:stream_side_data=rotation:format=duration",
                                "-of", "json", str(path)], capture_output=True, text=True, check=True)
            d = _json.loads(r.stdout)
            st = (d.get("streams") or [{}])[0]
            w, h = st.get("width", w), st.get("height", h)
            rot = next((abs(int(sd.get("rotation", 0))) for sd in st.get("side_data_list", []) if "rotation" in sd), 0)
            if rot in (90, 270):
                w, h = h, w
            if d.get("format", {}).get("duration"):
                dur = int(float(d["format"]["duration"]) * 1000)
        except Exception:
            pass
    thumb = None
    if shutil.which("ffmpeg"):
        with tempfile.TemporaryDirectory() as td:
            frame = pathlib.Path(td) / "f.jpg"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(path),
                            "-frames:v", "1", str(frame)], capture_output=True)
            if frame.exists():
                thumb = _thumb_and_size(frame)[0]
    return thumb, w, h, dur


def _media_cmd(kind: str):
    @click.option("--to", "target", required=True, help="me | g:<id> | u:<id> | <id>")
    @click.option("--caption", default=None)
    @click.option("--json", "as_json", is_flag=True)
    @click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path))
    def cmd(target, caption, as_json, path):
        from aiobale.types import FileInput

        async def go():
            c = _client()
            try:
                cid, ctype = await _resolve(c, target)
                f = FileInput(str(path))
                if kind == "file":
                    msg = await c.send_document(f, cid, ctype, caption=caption)
                elif kind == "video":
                    thumb, w, h, dur = _video_meta(path)
                    msg = await c.send_video(f, cid, ctype, caption=caption, cover_width=w,
                                             cover_height=h, duration=dur,
                                             cover_thumb=FileInput(thumb) if thumb else None)
                else:
                    thumb, w, h, dur = _thumb_and_size(path)
                    kw = dict(caption=caption, cover_width=w, cover_height=h,
                              cover_thumb=FileInput(thumb) if thumb else None)
                    if kind == "photo":
                        msg = await c.send_photo(f, cid, ctype, **kw)
                    else:
                        msg = await c.send_gif(f, cid, ctype, duration=dur, **kw)
            finally:
                await c.session.close()
            _sent(msg, as_json)
        _run(go())
    cmd.__doc__ = {"photo": "Send an image.", "gif": "Send a GIF/animation (plays inline).",
                   "file": "Send any file as a document.",
                   "video": "Send a video (plays inline, with thumbnail and duration)."}[kind]
    return cmd


send.command("photo")(_media_cmd("photo"))
send.command("gif")(_media_cmd("gif"))
send.command("file")(_media_cmd("file"))
send.command("video")(_media_cmd("video"))


if __name__ == "__main__":
    cli()
