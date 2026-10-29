# Пословные тайм-коды озвучки через OpenAI whisper-1.
#   python3 words.py vo.mp3 [язык=ru] [выход=vo_words.json]
# Ключ — переменная окружения OPENAI_API_KEY. Без зависимостей, кроме Python 3.
import json, os, sys, pathlib, urllib.request, urllib.error, uuid

if len(sys.argv) < 2: sys.exit('usage: python3 words.py vo.mp3 [ru] [vo_words.json]')
audio = pathlib.Path(sys.argv[1]); lang = sys.argv[2] if len(sys.argv) > 2 else 'ru'
out = sys.argv[3] if len(sys.argv) > 3 else 'vo_words.json'
key = os.getenv('OPENAI_API_KEY') or sys.exit('нет OPENAI_API_KEY')

b = uuid.uuid4().hex
parts = [('model', 'whisper-1'), ('language', lang), ('response_format', 'verbose_json'),
         ('timestamp_granularities[]', 'word'), ('timestamp_granularities[]', 'segment')]
body = b''.join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in parts)
body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{audio.name}"\r\n'
         f'Content-Type: application/octet-stream\r\n\r\n').encode() + audio.read_bytes() + f'\r\n--{b}--\r\n'.encode()
r = urllib.request.Request('https://api.openai.com/v1/audio/transcriptions', data=body, method='POST',
                           headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'multipart/form-data; boundary=' + b})
try: j = json.load(urllib.request.urlopen(r, timeout=300))
except urllib.error.HTTPError as e: sys.exit(f'ERR {e.code} {e.read()[:400].decode()}')
json.dump(j, open(out, 'w'), ensure_ascii=False, indent=1)
print(j['text']); print(len(j['words']), 'слов →', out)
