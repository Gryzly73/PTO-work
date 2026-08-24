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
FROM ${PYTHON_IMAGE}

# Tesseract нужен local_ocr.py: OCR углов штампа и авторотация повёрнутых
# листов через OSD. Без языковых пакетов rus+eng он бесполезен.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-rus \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

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
