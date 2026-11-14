import os, re
sp = r'B:\projects\TwitchTTS\TwitchTTS\venv\Lib\site-packages'
s = open(os.path.join(sp, 'TTS', 'tts', 'layers', 'xtts', 'tokenizer.py'), encoding='utf-8').read()
i = s.find('def japanese_cleaners')
print('--- XTTS japanese_cleaners ---')
print(s[i:i+600])
c = open(os.path.join(sp, 'cutlet', 'cutlet.py'), encoding='utf-8').read()
j = c.find('class Cutlet')
print('--- cutlet API ---')
print(c[j:j+1500])
