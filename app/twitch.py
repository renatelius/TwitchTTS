# -*- coding: utf-8 -*-
"""Анонимное чтение чата Twitch по IRC (логин и токен не нужны)."""
import asyncio
import logging
import random
import re

log = logging.getLogger("twitch")

HOST = "irc.chat.twitch.tv"
PORT = 6697
CHANNEL_RE = re.compile(r"^[a-z0-9_]{1,25}$")


def normalize_channel(value):
    """Принимает имя канала или ссылку twitch.tv/имя, возвращает имя или ''."""
    value = (value or "").strip().lower()
    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"^(www\.|m\.)?twitch\.tv/", "", value)
    value = value.split("/")[0].split("?")[0].lstrip("#@")
    return value if CHANNEL_RE.match(value) else ""


def _unescape_tag(value):
    return (value.replace("\\s", " ").replace("\\:", ";")
            .replace("\\r", "").replace("\\n", "").replace("\\\\", "\\"))


def parse_line(line):
    """Разбирает строку IRC. Возвращает dict(tags, prefix, command, params)."""
    tags = {}
    prefix = ""
    if line.startswith("@"):
        raw, _, line = line[1:].partition(" ")
        for item in raw.split(";"):
            key, _, val = item.partition("=")
            tags[key] = _unescape_tag(val)
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    trailing = None
    if " :" in line:
        line, _, trailing = line.partition(" :")
    parts = line.split()
    if not parts:
        return None
    params = parts[1:]
    if trailing is not None:
        params.append(trailing)
    return {"tags": tags, "prefix": prefix, "command": parts[0], "params": params}


def parse_privmsg(msg):
    """Из PRIVMSG делает удобный словарь сообщения чата."""
    tags = msg["tags"]
    text = msg["params"][-1] if msg["params"] else ""
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        text = text[8:-1]
    login = msg["prefix"].split("!")[0].lower()
    emotes = []
    for group in filter(None, tags.get("emotes", "").split("/")):
        _, _, ranges = group.partition(":")
        for rng in ranges.split(","):
            a, _, b = rng.partition("-")
            if a.isdigit() and b.isdigit():
                emotes.append((int(a), int(b)))
    badges = {b.split("/")[0] for b in tags.get("badges", "").split(",") if b}
    return {
        "id": tags.get("id", ""),
        "login": login,
        "name": tags.get("display-name") or login,
        "color": tags.get("color", ""),
        "text": text,
        "emotes": emotes,
        "is_mod": tags.get("mod") == "1" or "moderator" in badges,
        "is_broadcaster": "broadcaster" in badges,
        "is_sub": tags.get("subscriber") == "1" or "subscriber" in badges or "founder" in badges,
        "is_vip": "vip" in badges,
        # сообщение, отправленное вместе с наградой за баллы канала (награда с полем для текста)
        "is_reward": bool(tags.get("custom-reward-id")),
    }


class TwitchChat:
    def __init__(self, on_message, on_status):
        self.on_message = on_message
        self.on_status = on_status
        self.channel = ""
        self.status = "off"  # off | connecting | on | error
        self.detail = ""
        self._task = None

    def _set(self, status, detail=""):
        self.status, self.detail = status, detail
        self.on_status()

    async def connect(self, channel):
        await self.disconnect()
        self.channel = channel
        self._task = asyncio.create_task(self._run())

    async def disconnect(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._set("off")

    async def _run(self):
        delay = 2
        while True:
            writer = None
            try:
                self._set("connecting")
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(HOST, PORT, ssl=True), timeout=15)
                nick = "justinfan%d" % random.randint(10000, 99999)
                writer.write(("CAP REQ :twitch.tv/tags twitch.tv/commands\r\n"
                              "NICK %s\r\nJOIN #%s\r\n" % (nick, self.channel)).encode())
                await writer.drain()
                while True:
                    raw = await asyncio.wait_for(reader.readline(), timeout=330)
                    if not raw:
                        raise ConnectionError("соединение закрыто")
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("PING"):
                        writer.write(("PONG" + line[4:] + "\r\n").encode())
                        await writer.drain()
                        continue
                    msg = parse_line(line)
                    if not msg:
                        continue
                    cmd = msg["command"]
                    if cmd in ("JOIN", "ROOMSTATE") and self.status != "on":
                        delay = 2
                        self._set("on")
                    elif cmd == "PRIVMSG":
                        try:
                            self.on_message(parse_privmsg(msg))
                        except Exception:
                            log.exception("ошибка обработки сообщения")
                    elif cmd == "RECONNECT":
                        raise ConnectionError("Twitch попросил переподключиться")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("чат отключился: %r", e)
                self._set("error", "нет связи с Twitch, переподключаюсь… (%s)" % (e or type(e).__name__))
            finally:
                if writer:
                    writer.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
