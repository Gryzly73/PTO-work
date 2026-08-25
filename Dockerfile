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
# Сборка идёт отдельным слоем: в рабочий образ переезжают только сам dwg2dxf и
# его библиотека, а компилятор с заголовками остаются здесь.
FROM ${PYTHON_IMAGE} AS dwgtools
ARG LIBREDWG_VERSION=0.14
# pkg-config и libpcre2-dev — не подстраховка: без первого configure
# LibreDWG останавливается сразу («pkg-config not found»), второй нужен ему
# для разбора кодовых страниц. Оба остаются в этой стадии.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl pkg-config libpcre2-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl -fsSL -o libredwg.tar.xz \
        "https://github.com/LibreDWG/libredwg/releases/download/${LIBREDWG_VERSION}/libredwg-${LIBREDWG_VERSION}.tar.xz" \
    && tar xf libredwg.tar.xz \
    && cd "libredwg-${LIBREDWG_VERSION}" \
    && ./configure --disable-bindings --disable-dependency-tracking --prefix=/usr/local \
    && make -j"$(nproc)" \
    && make install-strip DESTDIR=/out \
    && /out/usr/local/bin/dwg2dxf --version | head -1

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
COPY --from=dwgtools /out/usr/local/lib/ /usr/local/lib/
RUN ldconfig && dwg2dxf --version | head -1
ENV PTO_DWG2DXF=/usr/local/bin/dwg2dxf

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
