#!/bin/bash
# Сведение: видео без звука + голос + музыка → готовый ролик.
#   bash mix.sh out/video.mp4 vo_fast.wav music.mp3 out/final.mp4 [громкость_музыки=0.35] [сдвиг_музыки_с=0]
# Музыка зацикливается на длину видео, приглушается под голосом (sidechain), в конце затухает 1,5 с.
# Итог нормализуется до −14 LUFS (норма Reels / TikTok / YouTube). Без музыки: передать "-" вместо music.mp3.
set -e
V="$1"; VO="$2"; MUS="$3"; OUT="$4"; MG="${5:-0.35}"; OFF="${6:-0}"
[ -z "$OUT" ] && { echo "usage: bash mix.sh video.mp4 vo.wav music.mp3|- out.mp4 [0.35] [0]"; exit 1; }
DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$V")
FO=$(echo "$DUR - 1.5" | bc)
if [ "$MUS" = "-" ]; then
  ffmpeg -v error -y -i "$V" -i "$VO" -filter_complex \
    "[1:a]apad,atrim=0:${DUR},loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]" \
    -map 0:v -map "[a]" -c:v copy -c:a aac -b:a 256k -movflags +faststart "$OUT"
else
  ffmpeg -v error -y -i "$V" -i "$VO" -ss "$OFF" -stream_loop -1 -i "$MUS" -filter_complex \
    "[1:a]aresample=48000,apad,atrim=0:${DUR},asplit[v1][v2];\
     [2:a]aresample=48000,atrim=0:${DUR},volume=${MG},afade=t=out:st=${FO}:d=1.5[m];\
     [m][v1]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=350[md];\
     [v2][md]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]" \
    -map 0:v -map "[a]" -c:v copy -c:a aac -b:a 256k -movflags +faststart "$OUT"
fi
echo "готово: $OUT"
# Проверка баланса: голос должен быть на 5–6 дБ громче музыки на участках с речью —
# послушать в наушниках и на динамике телефона; если музыка мешает — уменьшить громкость (5-й аргумент).
