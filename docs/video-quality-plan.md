# План: сохранение максимального качества видео AutoMintaje

Репозиторий: https://github.com/labiozzza/automintaje
Ветка: `quality-max`
Дата: 2026-10-22
Источник требований: ТЗ «Сохранение максимального качества видео AutoMintaje»

## 0. Неприкосновенные механизмы

- Автозум (`montage/apply_face_zoom_v5.py`, `montage/face_tracking.py`, `montage/camera_trajectory.py`, `montage/engine.py`) сохраняется как независимый модуль: алгоритмы, настройки, силы увеличения, траектории, визуальный результат, все режимы камеры и границ.
- Контент-фабрика (`factory_batch.py`, `factory_autoposter.py`) не затрагивается.
- Реальные публикации не выполняются ни в одном этапе.
- Все остальные функции (субтитры, подсветка слов, анимации, отражение, аудиоцензура, preview, обложки, подготовка IG/YT/TG, отмена/восстановление, прогресс, API) сохраняются.

## 1. Факты, подтверждённые кодом (аудит цепочки)

Цепочка монтажа (`montage/engine.py::render_montage`):

1. `prepare` — проверка исходника.
2. `rotate` (если 90/270) — `libx264 -crf 0 -preset fast -pix_fmt yuv420p`, аудио copy. Видео перекодируется один раз. `flags=lanczos` для scale.
3. `mirror` (если включено) — `libx264 -crf 0 -preset fast -pix_fmt yuv420p`, `hflip`, аудио copy.
4. `configs` — BigPickle генерирует zoom/subtitle config; без перекодирования.
5. `faces`/`zoom` — `apply_face_zoom_v5.py`: OpenCV читает кадры, записывает rawvideo bgr24 в ffmpeg stdin; ffmpeg `libx264 -crf <cfg, default 18> -preset <default veryfast> -pix_fmt yuv420p`, аудио copy. **Один lossy-проход с CRF 18 по умолчанию.**
6. `subtitles` — `apply_auto_subtitles_v2.py`: OpenCV + ffmpeg `libx264 -crf <cfg, default 18> -preset <default veryfast> -pix_fmt yuv420p`.
7. `animation` (если есть слои) — `apply_timeline_animation.py`: OpenCV + ffmpeg `libx264 -preset medium -crf 0 -pix_fmt yuv420p` (lossless в рамках yuv420p).
8. `audio_censor` (если включено) — `-c:v copy`, аудио AAC 192k. Без перекодирования видео.
9. `finalize` — `libx265 -crf 14 -preset medium -pix_fmt yuv420p` (мастер-результат, HEVC).
10. `preview` — `libx264 -crf 12 -preset medium -pix_fmt yuv420p` (H.264).

Подготовка к публикации (`app.py::_render_prepared_video`):

- YouTube: `YouTubePublisher.prepare_video` — `libx264 -crf 16 -preset medium -pix_fmt yuv420p`, принудительный CFR через `fps=<nearest standard>`, `setpts`/`aresample`. Полное перекодирование.
- Telegram: `montage_engine._transcode_to_fit` — `libx264 -b:v <calc> -pix_fmt yuv420p`, AAC 160k. Полное перекодирование.
- Instagram: `InstagramPublisher.prepare_variant` → `_normalize_for_publication` — `libx264 -crf 18 -preset medium -pix_fmt yuv420p`, `fps=30` принудительно, CFR. Затем при превышении 100 MB `_transcode_for_github` — ещё один проход `libx264 -b:v`.

Таким образом исходник проходит 3–5 lossy-кодирований (zoom CRF18, subtitles CRF18, finalize HEVC CRF14, preview H.264 CRF12, публикация H.264 CRF18). Плюс преобразование BGR→YUV420P на каждом OpenCV-этапе (chroma subsampling повторяется).

Подтверждённые факты о цвете:
- Исходники в тестовых данных: HEVC yuv420p, bt709/bt709/bt709, 30 fps, 1280x720. HDR-источников в тестовых данных нет (проверено ffprobe по всем `montage_work/*/project_video.MOV`).
- OpenCV читает кадры как BGR (8-bit), записывает bgr24; ffmpeg конвертирует в yuv420p на каждом этапе. Цветовые метаданные исходника (primaries/transfer/space) не передаются и не сохраняются.
- `ffprobe` доступен (v7.1.1), фильтры `libvmaf`, `psnr`, `ssim`, `xpsnr` доступны.

Подтверждённые факты об FPS:
- Instagram принудительно `fps=30` в `_normalize_for_publication` и `_transcode_for_github`.
- YouTube принудительно CFR с ближайшим стандартным fps (`_frame_rate`).
- Telegram не меняет fps (копирует через `_transcode_to_fit` с тем же потоком, но перекодирует).
- Автозум использует `frame_index / fps` из OpenCV `CAP_PROP_FPS`.

Типы: `engine.py` использует синтаксис `X | None` (Python 3.10+) в сигнатурах, хотя AGENTS.md заявляет Python 3.9. Фактически тесты проходят (вероятно, `from __future__ import annotations` или ленивая оценка). Требует проверки при изменениях.

## 2. Гипотезы, требующие измерений

- H1: Основная визуальная деградация возникает не от CRF (CRF 0/14/18), а от повторного chroma subsampling (yuv420p) и цветовой конверсии BGR↔YUV на каждом этапе. Требует измерения SSIM/VMAF между исходником и финалом.
- H2: Переход на yuv444p или 10-bit (yuv420p10le) для промежуточных этапов уменьшит деградацию без изменения алгоритма зума. Требует бенчмарка скорости/размера.
- H3: Stream copy возможен для части платформенных версий (когда исходный кодек/контейнер/FPS совместимы). Требует проверки совместимости по каждому случаю.
- H4: Сохранение исходного FPS для Instagram может нарушать требования API. Требует подтверждения документации IG Graph API.
- H5: HDR-источники (iPhone HLG/PQ) в текущем пайплайне конвертируются в SDR некорректно. В тестовых данных HDR-источников нет, поэтому гипотеза не верифицирована на реальных данных.

## 3. Стратегии (сравнение)

### Стратегия A — минимальные изменения кодирования и экспорта

Суть: не менять архитектуру, только поднять качество существующих проходов и убрать явно лишние.

Изменения:
- Zoom/subtitles: CRF 18 → 12 (или 0 для lossless-промежуточных), preset veryfast → medium, pix_fmt yuv420p → yuv444p (или yuv420p10le).
- Finalize: оставить HEVC CRF 14, но добавить `-color_primaries/transfer/space` из исходника.
- Preview: оставить H.264 CRF 12.
- Publishers: добавить stream copy при полной совместимости (codec/container/fps/dimensions/audio), иначе однократное кодирование из `final.mp4` (как сейчас, но с правильными параметрами).
- Instagram: убрать принудительный `fps=30` если API позволяет; иначе документировать.

Плюсы: минимальный риск, малый объём изменений, сохраняет весь API.
Минусы: не решает повторный chroma subsampling полностью (4 прохода остаются), не решает цветовые метаданные в промежуточных этапах.

### Стратегия B — высококачественный промежуточный мастер

Суть: ввести единый мастер-файл после всех визуальных эффектов; все конечные форматы (preview, IG, YT, TG, скачивание) формируются из мастера.

Изменения:
- После этапа `animation` (или `subtitles` если нет анимаций) создавать `master.mp4` в lossless/visually-lossless формате (кандидаты: FFV1, ProRes 422 HQ, HEVC CRF 10, x264 CRF 0 yuv444p) — выбор по бенчмарку.
- `finalize` (HEVC CRF14) и `preview` (H.264 CRF12) кодируются из мастера, а не из `final.mp4`.
- Publishers кодируют из мастера.
- Безопасное кеширование: мастер удаляется после завершения всех дериватов (или по флагу диагностики сохраняется).
- Профили качества (раздел 8 ТЗ) централизуют параметры.

Плюсы: один lossy-проход для визуальных эффектов, все дериваты из одного источника, меньше накопленной деградации.
Минусы: дополнительный файл на диске (до 10x размер), время кодирования мастера, риск несовместимости формата с downstream (ffmpeg должен уметь читать). Требует бенчмарка выбора формата.

### Стратегия C — частичное объединение рендера без изменения автозума

Суть: объединить zoom + subtitles + animation в один проход OpenCV→ffmpeg (один encode), автозум вызывается как функция преобразования кадра в общем цикле.

Изменения:
- Новый скрипт `apply_render_pipeline.py`, который читает кадр, вызывает `transform_frame` (из apply_face_zoom_v5), рисует субтитры (из apply_auto_subtitles_v2), накладывает анимации (из apply_timeline_animation), пишет в один ffmpeg.
- Автозум сохраняет API: `transform_frame`, `compute_camera_track`, `settings_from_dict` вызываются как есть.
- Один encode-проход вместо трёх.

Плюсы: минимальное число кодирований (1 pass для визуальных эффектов), максимальное сохранение качества.
Минусы: большой рефакторинг (объединение трёх скриптов), риск регрессии автозума, сложность тестирования. Требует регрессионной проверки геометрии зума.

## 4. Рекомендуемая стратегия

**Рекомендация: гибрид A → B → (C только при доказанной пользе).**

Обоснование:
1. Стратегия A решает 60-70% проблемы (CRF/preset/pix_fmt + stream copy + метаданные) при минимальном риске и минимальном объёме. Это первый этап (Этап B.1).
2. Стратегия B добавляет мастер-файл, устраняя накопленную деградацию от повторного chroma subsampling. Это второй этап (Этап B.2), после бенчмарка формата мастера.
3. Стратегия C (объединение рендера) — только если измерения покажут, что проходы B всё ещё дают заметную деградацию. Это Этап D, опциональный.

Причина не начинать с C: риск регрессии автозума и сложность тестирования перевешивают потенциальную выгоду. Причина не начинать с B: выбор формата мастера требует бенчмарка, который должен быть выполнен в рамках Этапа A (диагностика).

## 5. Этапы реализации

### Этап A — Диагностика (P0)

Цель: составить полный граф обработки, зафиксировать baseline-поведение автозума, создать инструмент сравнения.

Точные изменения:
1. Создать `montage/quality_audit.py` — диагностический инструмент: принимает список видеофайлов, запускает ffprobe для каждого, собирает метрики (codec, profile/level, fps, timebase, dimensions, pix_fmt, bit depth, color primaries/transfer/space/range, bitrate, duration, HDR metadata, audio codec/channels/sample rate). Опционально вычисляет PSNR/SSIM/VMAF между парой видео (эталон/результат) с учётом преобразований (зум/поворот/текст) — эталон готовится отдельно.
2. Добавить флаг `--keep-intermediates` (или env `AUTOMONTAGE_KEEP_INTERMEDIATES=1`) в `render_montage`, который отключает очистку промежуточных файлов (rotate.log, zoom.mp4 и т.д. уже сохраняются; нужно сохранять и сами mp4: `normalized_source.mp4`, `mirrored_source.mp4`, `sub_*.mp4`, `with_visual_layers.mp4`, `censored_audio.mp4`). По умолчанию поведение не меняется.
3. Сохранять FFmpeg-команды в `ffmpeg_commands.json` в workdir при диагностическом режиме.
4. Создать baseline-тесты автозума: зафиксировать для набора тестовых видео (без эффектов, только зум, зум+субтитры, зум+анимации, все эффекты) значения zoom factor по кадрам, положение центра кадрирования, события зума. Тест сравнивает рассчитанные траектории (не сжатое изображение).
5. Создать `quality_report.json` формат (схема) и пример.

Затрагиваемые файлы: новый `montage/quality_audit.py`, `montage/engine.py` (флаг keep-intermediates), `tests/test_quality_audit.py`, `tests/test_zoom_baseline.py`.

Зависимости: ffprobe/ffmpeg уже установлены. libvmaf доступен.

Риски: низкие. Только добавление функциональности.

План тестирования: unittest для quality_audit (парсинг ffprobe, вычисление метрик на тестовых файлах). Baseline-тесты зума на существующих test_camera_pipeline.

Критерии готовности: quality_audit запускается на паре файлов и выдаёт JSON; keep-intermediates работает; baseline-тесты проходят и зафиксированы.

Откат: удалить новый файл и флаг.

### Этап B.1 — Улучшение кодирования (P0/P1)

Цель: поднять качество существующих проходов без изменения архитектуры.

Точные изменения:
1. `apply_face_zoom_v5.py`: `start_ffmpeg` — добавить параметры `-color_primaries`, `-color_trc`, `-colorspace` из исходника (через ffprobe в начале скрипта, с fallback bt709/tv). CRF по умолчанию: 18 → 14? Или оставить конфигурируемым через `encoding.crf` (уже есть, default 18). Изменить default на 14? Решение: оставить default 18 (не ломать существующие тесты), но изменить конфиги, генерируемые `generate_configs_from_transcript_srt.py`, с `crf: 0` → `crf: 12`? Нет — `crf: 0` в zoom config означает lossless-промежуточный (что хорошо для zoom-этапа). Оставить `crf: 0` для zoom/subtitles (они уже lossless в рамках yuv420p). Изменить только pix_fmt: yuv420p → yuv444p для zoom/subtitles/animation (убирает chroma subsampling на промежуточных этапах). Preset veryfast → medium для zoom/subtitles.
2. `apply_auto_subtitles_v2.py`: аналогично pix_fmt yuv444p, preset medium.
3. `apply_timeline_animation.py`: уже crf 0; pix_fmt yuv420p → yuv444p.
4. `engine.py` rotate/mirror: pix_fmt yuv420p → yuv444p (crf 0 остаётся).
5. `engine.py` finalize (HEVC): добавить `-color_primaries/transfer/space` из исходника. CRF 14 остаётся (качественный мастер-результат). pix_fmt yuv420p остаётся (совместимость).
6. `engine.py` preview (H.264): добавить цветовые метаданные. CRF 12 остаётся.
7. `audio_censor`: `-c:v copy` уже есть, добавить цветовые метаданные если копирование не сохраняет (copy сохраняет).

Затрагиваемые файлы: `montage/apply_face_zoom_v5.py`, `montage/apply_auto_subtitles_v2.py`, `montage/apply_timeline_animation.py`, `montage/engine.py`.

Риски: средние. Изменение pix_fmt промежуточных файлов увеличивает размер (yuv444p ~1.5x). Нужно проверить, что downstream-этапы (subtitles читает zoom.mp4) корректно читают yuv444p. OpenCV читает любой pix_fmt. ffmpeg читает. Должно работать.

План тестирования: существующие тесты (test_camera_pipeline, test_montage_e2e, test_montage_unit) + новый тест на цветовые метаданные (ffprobe результата).

Критерии готовности: все тесты проходят; ffprobe финала показывает bt709 метаданные; промежуточные файлы yuv444p.

Откат: revert pix_fmt изменений.

### Этап B.2 — Мастер-файл и оптимизация экспорта (P0/P1)

Цель: ввести единый высококачественный мастер; платформенные версии из мастера.

Точные изменения:
1. В `render_montage`: после `animation` (или `subtitles`) создать `master.mp4` путём копирования `final_mp4` (который уже содержит все визуальные эффекты) или перекодирования в выбранный формат. Выбор формата — по результатам бенчмарка Этапа A (сравнить FFV1, ProRes 422 HQ, HEVC CRF 10, x264 CRF 0 yuv444p по качеству/скорости/размеру).
2. `finalize` (HEVC CRF14) и `preview` (H.264 CRF12) кодируются из `master.mp4`.
3. `delivery_mp4` = HEVC из мастера (скачиваемый результат).
4. `preview_mp4` = H.264 из мастера.
5. Publishers: читают `master.mp4` вместо `final.mp4` (или `result_video`). Добавить stream copy при полной совместимости.
6. Безопасное кеширование: мастер удаляется в конце `render_montage` (после создания дериватов) если `keep-intermediates` не установлен. При `keep-intermediates` мастер сохраняется.
7. `job_state.json`: добавить поле `master_video` (путь) для восстановления.

Затрагиваемые файлы: `montage/engine.py`, `app.py` (publish prep), `montage/instagram_publisher.py`, `montage/youtube_publisher.py`.

Риски: средние-высокие. Выбор формата мастера критичен. Дополнительный файл на диске. Нужна проверка совместимости downstream.

План тестирования: e2e тесты (test_montage_e2e, test_montage_prepare_e2e) + новый тест на то, что publishers читают мастер.

Критерии готовности: мастер создаётся и удаляется; дериваты создаются из мастера; качество не хуже baseline.

Откат: убрать мастер, вернуться к кодированию из `final.mp4`.

### Этап B.3 — Stream copy в публикациях (P0/P1)

Цель: убрать лишние перекодирования в IG/YT/TG.

Точные изменения:
1. `YouTubePublisher.prepare_video`: проверить совместимость (codec H.264, container MP4, fps стандартный, audio AAC). Если совместимо — `-c:v copy -c:a copy` (только перемуксинг с faststart). Иначе однократное кодирование из мастера.
2. `InstagramPublisher.prepare_variant`: аналогично. Если исходный поток совместим с IG (H.264, yuv420p, ≤1080p, fps ≤30) — copy. Иначе однократное кодирование из мастера с правильными параметрами (убрать принудительный fps=30 если API позволяет).
3. `montage_engine._transcode_to_fit` (Telegram): если файл уже ≤45MB и совместим — copy. Иначе кодировать из мастера с рассчитанным битрейтом.
4. Добавить функцию `is_stream_compatible(source, target)` в `montage/engine.py` (проверка codec, pix_fmt, fps, dimensions, audio codec).

Затрагиваемые файлы: `montage/youtube_publisher.py`, `montage/instagram_publisher.py`, `montage/engine.py`.

Риски: средние. Нужна точная проверка совместимости. Неправильный copy может дать несовместимый файл.

План тестирования: unit-тесты `is_stream_compatible`; e2e тесты подготовки.

Критерии готовности: stream copy используется когда возможно; перекодирование только при несовместимости.

Откат: вернуть полное перекодирование.

### Этап C — Цвет и FPS (P1)

Цель: корректная обработка цвета и сохранение FPS.

Точные изменения:
1. Создать `montage/color_utils.py`: функция `probe_color_params(path)` → {primaries, transfer, space, range, bit_depth}. Функция `color_args_for_ffmpeg(params)` → список аргументов `-color_primaries`, `-color_trc`, `-colorspace`, `-color_range` (или пустой если неизвестно).
2. Применить цветовые аргументы во всех ffmpeg-командах (rotate, mirror, zoom, subtitles, animation, finalize, preview, publishers).
3. HDR: если transfer в {smpte2084, arib-std-b67} (PQ/HLG) — использовать tone mapping (ffmpeg `zscale` + `tonemap`) или документировать controlled SDR export. По умолчанию: controlled SDR export с документированным преобразованием (tone mapping через ffmpeg `tonemap=hable` или `zscale`). Полный HDR-preserve не реализовывать (требует весь путь рендера в 10-bit, что конфликтует с OpenCV bgr24).
4. FPS: 
   - Instagram: проверить требования API. Если API поддерживает исходный FPS — убрать принудительный `fps=30`, использовать исходный FPS с CFR-нормализацией (setpts). Иначе оставить 30 с документацией.
   - YouTube: сохранять исходный FPS (уже делает через `_frame_rate`), но не округлять до стандартного если разница > 0.1 fps.
   - Telegram: сохранять исходный FPS (уже).
   - Автозум: использовать реальный fps из ffprobe, а не OpenCV `CAP_PROP_FPS` (может быть неточным для VFR). Для VFR — использовать средний fps или конвертировать в CFR с сохранением меток.
5. VFR: добавить детекцию VFR (сравнение r_frame_rate и avg_frame_rate). Если VFR — конвертировать в CFR с реальными метками (setpts) без дублирования кадров.

Затрагиваемые файлы: новый `montage/color_utils.py`, `montage/engine.py`, `montage/apply_face_zoom_v5.py`, `montage/apply_auto_subtitles_v2.py`, `montage/apply_timeline_animation.py`, `montage/instagram_publisher.py`, `montage/youtube_publisher.py`.

Риски: средние. Tone mapping может изменить цветовую палитру (требование: не менять палитру субтитров/анимаций — они накладываются после tone mapping, на SDR-результате, поэтому не затрагиваются).

План тестирования: unit-тесты color_utils; e2e тесты с SDR-источником (проверка метаданных).

Критерии готовности: ffprobe финала показывает корректные color_* метаданные; HDR-источник (если доступен) конвертируется с tone mapping.

Откат: убрать цветовые аргументы.

### Этап D — Оптимизация рендера (P1, опциональный)

Цель: сократить число проходов, если измерения Этапа A покажут необходимость.

Точные изменения (только если Этап A/B показали выгоду):
1. Создать `montage/apply_render_pipeline.py` — объединённый цикл: читает кадр, вызывает `transform_frame` (zoom), рисует субтитры, накладывает анимации, пишет в один ffmpeg.
2. Автозум сохраняет API: `transform_frame`, `compute_camera_track`, `settings_from_dict` вызываются как есть, без изменения.
3. Один encode-проход вместо трёх.

Затрагиваемые файлы: новый `montage/apply_render_pipeline.py`, `montage/engine.py` (вызов вместо трёх скриптов).

Риски: высокие. Регрессия автозума. Требует полной регрессионной проверки геометрии.

План тестирования: сравнение геометрии (zoom factor, anchor) между старым и новым пайплайном на одинаковых настройках. Визуальное сравнение кадров.

Критерии готовности: геометрия идентична; визуальный результат не хуже; все тесты проходят.

Откат: вернуть вызов трёх отдельных скриптов.

### Этап E — Регрессия и отчёт (P0)

Цель: полная проверка и документирование.

Точные изменения:
1. Прогнать все тесты (`unittest discover`).
2. Прогнать Playwright-сюиты (`test_*_ui.py`, `test_layout.py`, `test_publish_progress.py`).
3. Измерить метрики качества (VMAF/SSIM/PSNR) до/после на контрольных роликах.
4. Визуальное сравнение кадров (скриншоты ключевых моментов).
5. Проверка автозума (события, zoom factor, anchor, траектории).
6. Проверка синхронизации аудио/видео.
7. Проверка кеша и восстановления.
8. Написать итоговый отчёт (раздел 13 ТЗ).

Критерии готовности: все тесты проходят; метрики не хуже baseline; автозум идентичен; отчёт написан.

## 6. Профили качества (централизованная конфигурация)

Создать `montage/quality_profiles.py`:

```python
PROFILES = {
    "master": {"codec": "copy_or_lossless", "crf": 0, "preset": "medium", "pix_fmt": "yuv444p", "color": "source", "fps": "source", "audio": "copy"},
    "high_quality": {"codec": "libx265", "crf": 14, "preset": "medium", "pix_fmt": "yuv420p", "color": "source", "fps": "source", "audio": "copy"},
    "preview": {"codec": "libx264", "crf": 12, "preset": "medium", "pix_fmt": "yuv420p", "color": "source", "fps": "source", "audio": "copy"},
    "instagram": {"codec": "libx264", "crf": 18, "preset": "medium", "pix_fmt": "yuv420p", "color": "source", "fps": "platform", "audio": "aac_192k", "max_bytes": 100*1024*1024},
    "youtube": {"codec": "libx264", "crf": 16, "preset": "medium", "pix_fmt": "yuv420p", "color": "source", "fps": "source_cfr", "audio": "aac_192k"},
    "telegram": {"codec": "libx264", "crf": None, "preset": "medium", "pix_fmt": "yuv420p", "color": "source", "fps": "source", "audio": "aac_160k", "max_bytes": 45*1024*1024},
}
```

Профиль не управляет автозумом. Параметры автозума и кодирования независимы.

Инвалидация кеша: при изменении профиля quality (или его версии) пересчитывать `_montage_signature` / `_publish_prep_signature` (добавить `quality_profile` в `_MONTAGE_SIGNATURE_KEYS`).

В интерфейсе: добавить сведения о разрешении, FPS, выбранном качестве в результат монтажа (уже частично есть в `meta`). Не перегружать основной экран.

## 7. Критерии приёмки (из ТЗ, уточнённые)

1. Автозум визуально и функционально сохранён (события, zoom factor, anchor, траектории идентичны baseline).
2. Все режимы камеры (off/hold/live), субтитров, анимаций работают как прежде.
3. Устранены подтверждённые лишние перекодирования (stream copy где возможно, мастер-файл).
4. Платформенные версии формируются из качественного источника (мастер).
5. Цвет обрабатывается корректно (метаданные сохраняются, HDR→SDR документирован).
6. FPS сохраняется, если изменение не требуется (документировано где снижается).
7. Синхронизация аудио/видео не ухудшена.
8. Качество измерено инструментально (VMAF/SSIM/PSNR) и проверено визуально.
9. Работа приложения не нарушена (все тесты).
10. Автотесты пройдены (179+ существующих + новые).
11. Реальные публикации не выполнялись.

## 8. Риски и откат

- Риск регрессии автозума (Этап D): минимизирован тем, что D опционален и требует полной регрессии.
- Риск несовместимости pix_fmt yuv444p downstream: проверяется в Этапе B.1, откат — revert pix_fmt.
- Риск выбора формата мастера: бенчмарк в Этапе A, решение принимается до Этапа B.2.
- Риск увеличения времени рендера/размера файлов: измеряется в Этапе E.
- Риск HDR: контролируемый SDR-экспорт с документированием; полный HDR-preserve не реализуется.
EOF
echo "written"; wc -l docs/video-quality-plan.md