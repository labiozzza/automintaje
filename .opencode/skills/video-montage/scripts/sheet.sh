#!/bin/bash
# Контактные листы из stills/*.jpg (по 12 кадров) → sheet_0.jpg, sheet_1.jpg …
# Для вертикали 9:16 красным закрашены зоны, которые закрывает интерфейс Reels / TikTok / Shorts:
# бока x<90 и x>990, шапка y<270, низ y>1500, колонка кнопок x>900 на y 1100–1650 (в пикселях кадра 1080×1920).
# Важного текста в красном быть не должно. SAFE=0 — без разметки. Запуск: bash sheet.sh [папка_с_кадрами]
DIR="${1:-stills}"
files=("$DIR"/*.jpg); total=${#files[@]}
[ "$total" -eq 0 ] && { echo "нет кадров в $DIR"; exit 1; }
IFS=x read W H < <(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0:s=x "${files[0]}")
TW=360; TH=$(( H * TW / W / 2 * 2 ))
Z=""
if [ "${SAFE:-1}" = "1" ] && [ $(( H * 100 / W )) -gt 170 ]; then   # только для вертикали
  k() { echo $(( $1 * TW / 1080 )); }; v() { echo $(( $1 * TH / 1920 )); }
  Z=",drawbox=x=0:y=0:w=$(k 90):h=$TH:color=red@0.2:t=fill,drawbox=x=$(k 990):y=0:w=$(k 90):h=$TH:color=red@0.2:t=fill"
  Z+=",drawbox=x=$(k 90):y=0:w=$(k 900):h=$(v 270):color=red@0.2:t=fill,drawbox=x=$(k 90):y=$(v 1500):w=$(k 900):h=$(v 420):color=red@0.2:t=fill"
  Z+=",drawbox=x=$(k 900):y=$(v 1100):w=$(k 90):h=$(v 400):color=red@0.2:t=fill"
fi
rm -f sheet_*.jpg
s=0; i=0
while [ $s -lt $total ]; do
  part=("${files[@]:$s:12}"); n=${#part[@]}; args=(); fc=""; st=""; lay=""
  for kk in $(seq 0 $((n-1))); do
    args+=(-i "${part[$kk]}"); fc+="[${kk}]scale=${TW}:${TH}${Z}[s${kk}];"; st+="[s${kk}]"
    lay+="$(( (kk%6)*TW ))_$(( (kk/6)*TH ))|"
  done
  if [ "$n" -eq 1 ]; then ffmpeg -v error -y "${args[@]}" -vf "scale=${TW}:${TH}${Z}" -frames:v 1 sheet_${i}.jpg
  else ffmpeg -v error -y "${args[@]}" -filter_complex "${fc}${st}xstack=inputs=${n}:layout=${lay%|}:fill=white" -frames:v 1 sheet_${i}.jpg; fi
  echo "sheet_${i}.jpg"; s=$((s+12)); i=$((i+1))
done
