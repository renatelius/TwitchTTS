# -*- coding: utf-8 -*-
"""Синхронизирует встроенную копию style.css внутри poll.html.

Страница «Опрос» должна одинаково выглядеть и как вкладка приложения (с сервера),
и как отдельный файл, открытый с диска (file://). Поэтому стили в неё встроены.
После правки style.css запустите:  python tools/sync_poll_css.py
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, 'app', 'web', 'style.css')
POLL = os.path.join(ROOT, 'app', 'web', 'poll.html')

MARK = '/* Встроенная копия style.css:'
TAIL = '/* ============================ страница «Опрос» ============================ */'

css = io.open(CSS, encoding='utf-8').read()
poll = io.open(POLL, encoding='utf-8').read()

i = poll.index(MARK)
j = poll.index(TAIL, i)

poll = poll[:i] + MARK + ''' страница должна выглядеть одинаково и как вкладка приложения,
   и как отдельный файл, открытый с диска. Копия обновляется скриптом tools/sync_poll_css.py. */
''' + css + '\n' + poll[j:]

io.open(POLL, 'w', encoding='utf-8', newline='').write(poll)
print('poll.html: стили синхронизированы, символов:', len(poll))
