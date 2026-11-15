# -*- coding: utf-8 -*-
"""Запросы к Twitch API (Helix): награды, подписчики, таймауты.

Нужны Client ID и OAuth-токен Twitch. Без них работает всё, что не требует
API: чат, голоса, стили, события из чата.
"""
import json
import urllib.parse
import urllib.request

HELIX = "https://api.twitch.tv/helix"


class TwitchApiError(Exception):
    """Ошибка запроса к Twitch API."""


class TwitchApi:
    def __init__(self, client_id="", token="", timeout=10):
        self.client_id = (client_id or "").strip()
        self.token = (token or "").strip()
        self.http_timeout = timeout

    @property
    def ready(self):
        return bool(self.client_id and self.token)

    def request(self, method, path, params=None, body=None):
        if not self.ready:
            raise TwitchApiError("не задан Client ID или OAuth-токен Twitch")
        url = HELIX + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Client-Id", self.client_id)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout) as resp:
                raw = resp.read().decode("utf-8")
        except Exception as e:
            raise TwitchApiError(str(e))
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise TwitchApiError("Twitch API вернул не-JSON")

    # ---------- пользователи ----------
    def user_id(self, login):
        """id пользователя по нику (пусто, если не найден)."""
        if not login:
            return ""
        data = self.request("GET", "/users", {"login": login})
        items = data.get("data") or []
        return items[0].get("id", "") if items else ""

    def user_name(self, user_id):
        """Ник пользователя по id."""
        if not user_id:
            return ""
        data = self.request("GET", "/users", {"id": user_id})
        items = data.get("data") or []
        return items[0].get("login", "") if items else ""

    # ---------- награды ----------
    def rewards(self, broadcaster_id):
        """Список наград за баллы канала."""
        data = self.request("GET", "/channel_points/custom_rewards", {"broadcaster_id": broadcaster_id})
        return data.get("data") or []

    # ---------- подписчики ----------
    def followers(self, broadcaster_id, first=100):
        """Подписчики канала."""
        data = self.request("GET", "/channels/followers",
                            {"broadcaster_id": broadcaster_id, "first": first})
        return data.get("data") or []

    # ---------- модерация ----------
    def timeout(self, broadcaster_id, user_id, seconds=600, reason=""):
        """Таймаут зрителя (нужен модераторский токен)."""
        body = {"data": {"user_id": user_id, "duration": int(seconds)}}
        if reason:
            body["data"]["reason"] = reason
        return self.request("POST", "/moderation/bans",
                            {"broadcaster_id": broadcaster_id, "moderator_id": broadcaster_id}, body)
