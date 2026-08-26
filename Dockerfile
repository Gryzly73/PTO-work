# Образ бэкенда: конвейер PDF → Markdown и его HTTP-сервис.
#
# Логика запуска та же, что у run-local.bat: настройки берутся из backend/.env
# (в compose он подключается через env_file), пути задаются переменными,
# готовность проверяется по /health.
# Базовый образ берём НЕ с Docker Hub. Анонимные скачивания там ограничены по
# IP, и деплой падал на ровном месте: «429 Too Many Requests» при обычной
# пересборке, притом что сам образ не менялся. public.ecr.aws/docker/library —
# официальное зеркало Docker Official Images от AWS, те же образы, без лимита.
# Переопределяется сборочным аргументом: docker build --build-arg PYTHON_IMAGE=...
ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim

# --- конвертер DWG → DXF ----------------------------------------------------
# Чертежи приходят в DWG, а читаем мы DXF. Конвертер приходится собирать
# самим: LibreDWG нет ни в одном выпуске Debian (проверено по packages.debian.org
# — пусто во всех, включая sid), а готовых сборок под Linux проект не выкладывает,
# только исходники. ODA File Converter конвертирует полнее, но тянет Qt,
# запускается лишь через виртуальный экран и требует принять их EULA — для
# сервиса, который должен просто работать, это плохой размен.
#
# Собирается он НЕ здесь, а отдельным образом (Dockerfile.dwgtools), который
# GitHub Actions кладёт в GHCR. Причина простая: на сервере эта сборка не
# доходила до конца — на файле decode2.c машина уходила в своп и обрывала
# SSH-сессию деплоя вместе со сборкой. Здесь остаётся только взять готовое.
#
# Локально можно подставить свой образ:
#   docker build -f Dockerfile.dwgtools -t pto-dwgtools:local .
#   docker build --build-arg DWGTOOLS_IMAGE=pto-dwgtools:local -t pto-backend .
ARG DWGTOOLS_IMAGE=ghcr.io/sorryprod/pto-dwgtools:0.14
FROM ${DWGTOOLS_IMAGE} AS dwgtools

FROM ${PYTHON_IMAGE}

# Системные пакеты ставятся ДО конвертера: dwg2dxf слинкован с libpcre2, и
# проверка `dwg2dxf --version` без неё падала бы прямо на сборке.
#
# Tesseract нужен local_ocr.py: OCR углов штампа и авторотация повёрнутых
# листов через OSD. Без языковых пакетов rus+eng он бесполезен.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpcre2-8-0 \
        tesseract-ocr \
        tesseract-ocr-rus \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Конвертер и его библиотека. Путь кладём в PTO_DWG2DXF — dwg_sheets.py ищет
# сначала там, и на сервере поиск по PATH уже не нужен.
COPY --from=dwgtools /out/usr/local/bin/dwg2dxf /usr/local/bin/dwg2dxf
# dwgread читает DWG напрямую. Нужен для листов, которые конвертер теряет
# целиком: их окна вида берутся из исходника (dwg_direct.py). Без него разбор
# не падает — лист просто остаётся помеченным как потерянный.
COPY --from=dwgtools /out/usr/local/bin/dwgread /usr/local/bin/dwgread
COPY --from=dwgtools /out/usr/local/lib/ /usr/local/lib/
RUN ldconfig && dwg2dxf --version | head -1 && dwgread --version | head -1
ENV PTO_DWG2DXF=/usr/local/bin/dwg2dxf
ENV PTO_DWGREAD=/usr/local/bin/dwgread

WORKDIR /app

# Два файла копируются по отдельности не для красоты: COPY с несколькими
# источниками кладёт их в каталог назначения ПЛОСКО, и service/requirements.txt
# затирает корневой — сборка падала на «No such file: deps/service/requirements.txt».
COPY requirements.txt ./deps/requirements.txt
COPY service/requirements.txt ./deps/service/requirements.txt
RUN pip install --no-cache-dir -r deps/requirements.txt \
    && pip install --no-cache-dir -r deps/service/requirements.txt

COPY . /app

# Пути внутри контейнера. Локально те же переменные указывают на папки диска —
# это единственное, чем отличаются два способа запуска.
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PTO_SERVICE_HOST=0.0.0.0 \
    PTO_SERVICE_PORT=8000 \
    PTO_STATE_DIR=/state \
    PTO_RUNS_DIR=/runs \
    PTO_ALLOWED_PDF_ROOTS=/data/uploads

RUN mkdir -p /state /runs /data/uploads

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "service"]
