# -*- coding: utf-8 -*-
"""Полоса синтеза: одна задача за раз, важные идут первыми.

Модель одна, поэтому одновременно два синтеза не идут никогда: иначе видеопамять
не выдерживает (XTTS и CosyVoice вместе - почти вся карта).

Но предпрослушивание голосов на странице «Голоса» - это ручное действие, оно
не должно задерживать озвучку чата и наоборот. Поэтому у полосы два приоритета:
реплика чата ждёт не больше одной предпрослушительной фразы, а предпрослушивание
ждёт, пока очередь чата не пуста.

Модуль ничего не знает про модели: он только раздаёт «слоты» синтеза.
"""
import threading


class SynthLane:
    """Очередь синтеза: один слот, приоритет реплик чата выше."""

    def __init__(self):
        self._cv = threading.Condition(threading.Lock())
        self._busy = False
        self._waiting_chat = 0

    def acquire(self, chat=False):
        """Занимает слот синтеза. chat=True - реплика чата (высокий приоритет)."""
        with self._cv:
            if chat:
                self._waiting_chat += 1
            try:
                while self._busy or (not chat and self._waiting_chat > 0):
                    self._cv.wait(0.05)
            finally:
                if chat:
                    self._waiting_chat -= 1
            self._busy = True

    def release(self):
        """Освобождает слот: следующая задача может начаться."""
        with self._cv:
            self._busy = False
            self._cv.notify_all()

    def busy(self):
        with self._cv:
            return self._busy

    def waiting_chat(self):
        """Сколько реплик чата ждут слот (для диагностики и тестов)."""
        with self._cv:
            return self._waiting_chat
