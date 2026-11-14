# -*- coding: utf-8 -*-
"""Определение языка сообщения: чтобы голос читал не только по-русски.

XTTS v2 умеет 17 языков: en, es, fr, de, it, pt, pl, tr, ru, nl, cs, ar, zh-cn,
hu, ko, ja, hi. Остальные читаются ближайшим родственным: украинский и
белорусский - русским, греческий, иврит, тайский - английским.

Определение идёт по двум признакам: набору букв (алфавит) и частотным словам.
Ничего лишнего не устанавливается - всё работает офлайн.
"""

# Языки, которые умеет XTTS v2 (см. config.json модели).
XTTS_LANGS = ("en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs",
              "ar", "zh-cn", "hu", "ko", "ja", "hi")

# Языки, которые читает CosyVoice 3: модель китайско-английская, корейский
# и японский ею не читаются (проверено: на корейском выдаёт зависший слог).
# Такие куски озвучивает XTTS - тембр остаётся тем же голосом.
COSY_LANGS = ("ru", "en", "zh")


def split_by_supported(segs, supported):
    """Делит куски текста на группы по тому, умеет ли движок этот язык.

    Возвращает список пар (умеет, куски). Эмоция достаётся к предыдущему
    куску: у CosyVoice это его собственный тег, у XTTS - запись из sounds/.
    """
    groups = []
    for kind, val in segs:
        if kind == "emote":
            if groups:
                groups[-1][1].append((kind, val))
                continue
            is_ok = True
        else:
            is_ok = kind in supported
        if not groups or groups[-1][0] != is_ok:
            groups.append((is_ok, []))
        groups[-1][1].append((kind, val))
    return groups


# Язык -> ближайший поддерживаемый XTTS.
FALLBACK = {
    "uk": "ru", "be": "ru", "bg": "ru", "sr": "ru", "mk": "ru", "kk": "ru",
    "el": "en", "he": "en", "th": "en", "vi": "en", "id": "en", "ro": "en",
    "da": "en", "nb": "en", "no": "en", "fi": "en", "sv": "en", "lt": "en",
    "lv": "en", "et": "en", "sl": "en", "is": "en", "sq": "en", "ms": "en",
    "zh": "zh-cn",
}

# Частотные слова: по ним отличаются языки с одинаковым алфавитом.
STOP = {
    "en": "the of and to in is you that it for on with as was at be this have not but they i he she we my all so do me up out what how are hello hi good night day yeah yeah gg wp ok now".split(),
    "es": "el la los las que de y en un una por con para es no se su lo mi más como pero sus le ya o este sí porque hola qué tal wena qué más aquí todo bien buenos Salud".split(),
    "fr": "le la les de des du et un une que pour dans est pas sur avec je tu il ce plus au aux nous vous ça son ses bonjour salut merci comment allez va ça".split(),
    "de": "der die das und ist nicht ein eine zu den mit sich auf für ich du es wir auch aber noch nur wenn hallo guten tag morgen abend wie geht dir mich dich euch hier da jetzt heute gestern sehen sagen machen gehen kommen".split(),
    "it": "il lo la che di e un una per non con sono mi tu si ma le come più cosa questo qui quando anche ciao sto bene grazie".split(),
    "pt": "o a os as que de e um uma para com não é no na eu você mais por mas como muito também foi olá ola voce está saude obrigado eae".split(),
    "nl": "de het een en is van ik je niet dat op te met voor zijn was er maar om ook nog zich hoi hallo hoe gaat".split(),
    "pl": "nie się jest na to że do i w z jak tak co po ale za od takie tylko już bardzo siema elo hej dzień".split(),
    "cs": "a v se na je že o s z do to ale jako tak pro po já byl nebo jsem ten ahoj čau máš den".split(),
    "tr": "bir bu ve için ile da de çok ne o ben sen daha nasıl var yok her şey selam merhaba nasılsın naber teşekkür iyi".split(),
    "ru": "и в не на я что он с как а то все она так его но вы да по из же это привет спасибо пока здравствуйте".split(),
    "uk": "і в не на я що він з як а то все вона так його але ви та за це привіт бувай па дякую".split(),
    "sv": "och att det som en på är av för med den till inte har om ett men vi jag hej tack mår".split(),
    "da": "og at det en den til er som på de med han af ikke men om vi jeg hej tak".split(),
    "nb": "og jeg det at en et den til er som på de med han av ikke men vi hei tak".split(),
    "fi": "ja on ei se että joka olla hän mutta kuin niin tai jos kun ovat myös vain hei moi kiitos".split(),
    "hu": "a az és hogy nem is de egy meg el ki be fel le van ez már csak szia köszi".split(),
    "vi": "và của có là không được trong cho người những một các với đã khi xin chào thế giới hôm nay".split(),
    "id": "yang dan di itu dengan untuk tidak ini dari dalam akan pada saya juga halo selamat".split(),
    "ro": "și de la în cu un care este pe nu se pentru din mai ca dar ce salut mulțumesc".split(),
    "el": "και το της των στο με για από δεν είναι να η ο τα σε ένα γεια σου χαίρω".split(),
    "he": "של את לא על עם זה היא הוא אני אנחנו כי גם אבל רק כל אשר שלום תודה".split(),
    "th": "และ ที่ การ เป็น ใน มี ได้ ไม่ ให้ กับ จะ ก็ แต่ ว่า สวัสดี ครับ".split(),
    "ar": "في من على إلى عن مع هذا هذه التي الذي ما لا قد كل هو هي مرحبا شكرا".split(),
    "hi": "है और का की के में से हैं यह वह नहीं को पर एक भी कर लिए नमस्ते धन्यवाद".split(),
}

# Слова -> язык (обратный индекс STOP).
WORD_LANG = {}
for _lang, _words in STOP.items():
    for _w in _words:
        WORD_LANG.setdefault(_w, set()).add(_lang)

# Буквы, по которым язык узнаётся однозначно.
MARKS = {
    "uk": set("іїєґ"), "bg": set("ъщ"), "sr": set("ђћџ"), "be": set("ўі"),
    "kk": set("әғқңөұ"), "mk": set("ѓќѕџљњ"), "pl": set("ł"),
    "cs": set("řůěščřžýáíé"), "tr": set("ğışçöü"), "de": set("ß"),
    "sv": set("åäö"), "da": set("æøå"), "nb": set("æøå"), "fi": set("äö"),
    "vi": set("ăâđêôơư"), "ro": set("ăâîșț"),
}

# Алфавиты по диапазонам Unicode.
RANGES = [
    ("cyr", 0x0400, 0x04FF), ("greek", 0x0370, 0x03FF),
    ("hebrew", 0x0590, 0x05FF), ("arabic", 0x0600, 0x06FF),
    ("arabic", 0x0750, 0x077F), ("devanagari", 0x0900, 0x097F),
    ("thai", 0x0E00, 0x0E7F), ("latin", 0x0041, 0x024F),
    ("greek", 0x1F00, 0x1FFF), ("cyr", 0x0500, 0x052F),
    ("han", 0x3400, 0x4DBF), ("han", 0x4E00, 0x9FFF),
    ("hiragana", 0x3040, 0x309F), ("katakana", 0x30A0, 0x30FF),
    ("katakana", 0x31F0, 0x31FF), ("hangul", 0x1100, 0x11FF),
    ("hangul", 0x3130, 0x318F), ("hangul", 0xAC00, 0xD7AF),
]

# Язык для целого алфавита.
SCRIPT_LANG = {"han": "zh", "hiragana": "ja", "katakana": "ja",
                "hangul": "ko", "arabic": "ar", "devanagari": "hi",
                "greek": "el", "hebrew": "he", "thai": "th"}


# Алфавит по коду языка - нужно, чтобы понять, куда переводить незнакомый язык.
LANG_SCRIPT = {
    "ru": "cyr", "uk": "cyr", "be": "cyr", "bg": "cyr", "sr": "cyr", "mk": "cyr",
    "kk": "cyr", "el": "greek", "he": "hebrew", "ar": "arabic", "hi": "devanagari",
    "th": "thai", "zh": "han", "ja": "han", "ko": "hangul",
}


def script_of_lang(code):
    """Алфавит языка по его коду."""
    return LANG_SCRIPT.get((code or "").lower(), "latin")


def script_of(word):
    """Алфавит слова: latin, cyr, han, kana, hangul, arabic, dev, greek..."""
    for ch in word:
        code = ord(ch)
        for name, lo, hi in RANGES:
            if lo <= code <= hi:
                if name == "katakana" and any(0x3040 <= ord(c) <= 0x309F for c in word):
                    return "hiragana"
                return name
    return ""


def dominant_script(text):
    """Самый частый алфавит в тексте."""
    counts = {}
    for ch in text:
        code = ord(ch)
        for name, lo, hi in RANGES:
            if lo <= code <= hi:
                counts[name] = counts.get(name, 0) + 1
                break
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _mark_lang(word):
    """Язык по характерной букве: ї - украинский, ъ - болгарский и т.п."""
    for lang, marks in MARKS.items():
        if any(ch in marks for ch in word):
            return lang
    return ""


def detect(text):
    """Основной язык текста: код вида ru, de, ja."""
    if not text or not text.strip():
        return "en"
    script = dominant_script(text)
    if script and script not in ("latin", "cyr"):
        return SCRIPT_LANG.get(script, "en")
    words = [w.strip(".,!?;:\"'()[]{}<>").lower() for w in text.split()]
    words = [w for w in words if w]
    scores = {}
    for w in words:
        for lang in WORD_LANG.get(w, ()):
            scores[lang] = scores.get(lang, 0) + 1
    if scores:
        best = max(scores.items(), key=lambda kv: (kv[1], kv[0] == "ru"))
        if best[1] > 0:
            return best[0]
    if script == "cyr":
        mark = _mark_lang(" ".join(words))
        return mark or "ru"
    return "en"


def word_code(word, dominant="en"):
    """Язык отдельного слова - чтобы в смешанном сообщении звучало верно."""
    script = script_of(word)
    if script and script not in ("latin", "cyr"):
        return SCRIPT_LANG.get(script, dominant)
    clean = word.strip(".,!?;:\"'()[]{}<>").lower()
    langs = WORD_LANG.get(clean)
    if langs and len(langs) == 1:
        return next(iter(langs))
    if script == "cyr":
        mark = _mark_lang(clean)
        if mark:
            return mark
    return dominant


def to_xtts(code):
    """Код языка -> код, который понимает XTTS (иначе синтез упадёт на ассерте)."""
    code = (code or "en").lower()
    if code in XTTS_LANGS:
        return code
    return FALLBACK.get(code, "en")
