# Картинки через OpenAI Images API.
#   Новая:   python3 gen_image.py img/name "промпт" [1024x1536|1024x1024|1536x1024] [low|medium|high] [auto|transparent]
#   Правка:  python3 gen_image.py img/name_night "что изменить, композицию сохранить" 1024x1536 medium --edit img/name.png[,ref.png]
# Ключ — OPENAI_API_KEY; модель — env IMG_MODEL (по умолчанию gpt-image-2). Пишет name.png.
# Правка (images/edits) держит композицию: та же сцена ночью / без света / после — для переходов «было → стало».
import base64, json, os, pathlib, sys, urllib.error, urllib.request, uuid

args = [a for a in sys.argv[1:] if a != '--edit']
edit = sys.argv[sys.argv.index('--edit') + 1] if '--edit' in sys.argv else None
if edit: args.remove(edit)
if len(args) < 2: sys.exit('usage: python3 gen_image.py img/name "prompt" [size] [quality] [bg] [--edit src.png]')
name, prompt = args[0], args[1]
size = args[2] if len(args) > 2 else '1024x1536'
quality = args[3] if len(args) > 3 else 'low'
bg = args[4] if len(args) > 4 else 'auto'
key = os.getenv('OPENAI_API_KEY') or sys.exit('нет OPENAI_API_KEY')
model = os.getenv('IMG_MODEL', 'gpt-image-2')
pathlib.Path(name).parent.mkdir(parents=True, exist_ok=True)

if edit:
    b = uuid.uuid4().hex
    parts = [('model', model), ('prompt', prompt), ('size', size), ('quality', quality), ('n', '1')]
    body = b''.join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in parts)
    for i, f in enumerate(edit.split(',')):   # первая — главная, остальные — эталоны (персонаж, стиль)
        body += (f'--{b}\r\nContent-Disposition: form-data; name="image[]"; filename="src{i}.png"\r\n'
                 f'Content-Type: image/png\r\n\r\n').encode() + pathlib.Path(f).read_bytes() + b'\r\n'
    body += f'--{b}--\r\n'.encode()
    url, headers = 'https://api.openai.com/v1/images/edits', {'Content-Type': 'multipart/form-data; boundary=' + b}
else:
    j = {'model': model, 'prompt': prompt, 'size': size, 'quality': quality, 'background': bg, 'n': 1}
    if bg == 'transparent': j['output_format'] = 'png'
    body, url, headers = json.dumps(j).encode(), 'https://api.openai.com/v1/images/generations', {'Content-Type': 'application/json'}

r = urllib.request.Request(url, data=body, method='POST', headers={'Authorization': 'Bearer ' + key, **headers})
try: res = json.load(urllib.request.urlopen(r, timeout=300))
except urllib.error.HTTPError as e: sys.exit(f'ERR {e.code} {e.read()[:500].decode()}')
pathlib.Path(f'{name}.png').write_bytes(base64.b64decode(res['data'][0]['b64_json']))
print('ok', f'{name}.png', model, json.dumps(res.get('usage')))
