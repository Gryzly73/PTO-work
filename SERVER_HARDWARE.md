# Сервер ИИ — железо и нагрузка

Снимок: **2026-07-07 16:33 MSK**  
Хост: `iiuser@iivm` (`/home/iiuser/opencode_web/opencode_only`)

> Обновить: `bash scripts/collect_server_hardware.sh` (если добавлен) или блок команд в конце файла.

---

## Краткий вывод

| Параметр | Значение |
|----------|----------|
| GPU | **4× NVIDIA A16** (~15,3 GB VRAM каждая) |
| Суммарная VRAM | **~61 GB** (tensor parallel) |
| RAM | **62 GB** (свободно ~44 GB) |
| vLLM | `Qwen3.5-35B-A3B-GPTQ-Int4`, TP=4, context **32k** |
| Занятость VRAM | **~96% на каждой карте** (~14,7 / 15,3 GB) |
| Запас VRAM | **~240 MB на GPU** — практически нет |

**Для XCA AI:** текущая конфигурация выжимает железо под MoE Int4. Переход на **27B без кванта (BF16)** теоретически возможен на 4×A16, но **KV-cache при 32k** может не влезть без уменьшения `max-model-len` или `gpu-memory-utilization`. **9B BF16/FP16** влезет с большим запасом и даст максимум «плюшек» vLLM (prefix cache, speculative decoding). **27B AWQ/GPTQ** — компромисс: лучше качество, чем 9B, и много свободной VRAM под оптимизации.

---

## GPU

```
NVIDIA-SMI 570.211.01  |  Driver 570.211.01  |  CUDA 12.8

GPU 0: NVIDIA A16  |  14728 / 15356 MiB  |  util 0%  |  42°C
GPU 1: NVIDIA A16  |  14726 / 15356 MiB  |  util 0%  |  41°C
GPU 2: NVIDIA A16  |  14736 / 15356 MiB  |  util 0%  |  56°C
GPU 3: NVIDIA A16  |  14724 / 15356 MiB  |  util 0%  |  60°C

Процессы:
  VLLM::Worker_TP0..TP3  (PID 3514201–3514204)  ~14,7 GB на GPU
```

**Особенности A16:** карта для виртуализации / inference, 16 GB, низкий TDP (~62 W). Четыре карты в **tensor parallel (TP=4)** — единый пул ~61 GB.

---

## vLLM (текущий запуск)

```bash
/home/iiuser/vllm-env/bin/vllm serve Qwen/Qwen3.5-35B-A3B-GPTQ-Int4 \
  --host 0.0.0.0 --port 8000 \
  --tensor-parallel-size 4 \
  --gpu-memory-utilization 0.80 \
  --max-model-len 32768 \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --trust-remote-code \
  --dtype auto \
  --quantization moe_wna16
```

| Параметр | Значение |
|----------|----------|
| Модель | `Qwen/Qwen3.5-35B-A3B-GPTQ-Int4` (MoE, GPTQ Int4) |
| TP | 4 |
| Порт | 8000 |
| Context | 32768 |
| Tool calling | `qwen3_coder` |
| Сессия | tmux `vllm` (с ~2026-06-22) |

OpenCode (`opencode.json`): `vllm/Qwen3.5-35B-A3B-GPTQ-Int4`, baseURL `http://127.0.0.1:8000/v1`.

---

## RAM и Docker

```
RAM:  62 Gi total, 17 Gi used, 44 Gi available
Swap: 8 Gi (не используется)

xca-opencode:  ~1.5 GiB RAM, CPU ~9%
```

Память хоста не узкое место; узкое место — **VRAM под модель + KV-cache**.

---

## Оценка моделей на этом железе

Ориентиры для **4× A16, TP=4**, context как сейчас (до 32k):

| Модель | Веса (оценка) | VRAM | Реалистичность | Комментарий для XCA AI |
|--------|---------------|------|----------------|------------------------|
| **Qwen3.5-35B-A3B GPTQ** (сейчас) | MoE Int4 | ~59 GB | ✅ Работает | Почти весь VRAM; мало места под рост context/батча |
| **Qwen3.6 9B BF16/FP16** | ~18 GB | ~5 GB/GPU | ✅ С запасом | Быстро, много VRAM под prefix cache; риск по tool calling и длинным правилам |
| **Qwen3.6 27B AWQ/GPTQ Int4** | ~8–10 GB | ~3 GB/GPU | ✅ Вероятно лучший кандидат | Качество ближе к крупным моделям + запас под vLLM-оптимизации |
| **Qwen3.6 27B BF16** | ~54 GB | ~14 GB/GPU | ⚠️ На грани | Веса влезут; **KV при 32k** может OOM — тест с `max-model-len` 16k–24k |
| **Qwen3.6 27B BF16 + 32k** | — | — | ❌ Скорее нет | Без уменьшения context или без 2-й ступени кванта KV |

**Рекомендация (продукт):** не уходить сразу на 9B как единственную прод-модель. Сначала A/B: текущая MoE vs **27B AWQ** (или 27B BF16 с `max-model-len 16384`). 9B — как быстрый режим, если A/B покажет приемлемое качество на `DEMO_PRESENTATION.md`.

---

## Команды для повторного снимка

Выполнить на сервере ИИ:

```bash
cd ~/opencode_web/opencode_only
{
  echo "# Снимок $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo
  echo "## nvidia-smi"
  nvidia-smi
  echo
  echo "## GPU CSV"
  nvidia-smi --query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu --format=csv
  echo
  echo "## vLLM"
  ps aux | grep -E 'vllm serve' | grep -v grep
  echo
  echo "## RAM"
  free -h
  echo
  echo "## Docker"
  docker stats xca-opencode --no-stream 2>/dev/null || true
  echo
  echo "## Models API"
  curl -s http://127.0.0.1:8000/v1/models 2>/dev/null | head -c 600
  echo
} | tee /tmp/xca_hw_snapshot.txt
```

Скопировать вывод в этот файл (секция «Сырые данные» ниже) при смене модели или железа.

---

## Сырые данные (2026-07-07)

<details>
<summary>nvidia-smi, процессы, free, docker stats</summary>

```
Tue Jul  7 16:33:43 2026
+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 570.211.01             Driver Version: 570.211.01     CUDA Version: 12.8     |
|-----------------------------------------+------------------------+----------------------+
| GPU  Name                 Persistence-M | Bus-Id          Disp.A | Volatile Uncorr. ECC |
| Fan  Temp   Perf          Pwr:Usage/Cap |           Memory-Usage | GPU-Util  Compute M. |
|   0  NVIDIA A16                     Off |   00000000:03:00.0 Off |  14728MiB / 15356MiB | 0%  42C
|   1  NVIDIA A16                     Off |   00000000:05:00.0 Off |  14726MiB / 15356MiB | 0%  41C
|   2  NVIDIA A16                     Off |   00000000:0D:00.0 Off |  14736MiB / 15356MiB | 0%  56C
|   3  NVIDIA A16                     Off |   00000000:16:00.0 Off |  14724MiB / 15356MiB | 0%  60C
+-----------------------------------------------------------------------------------------+
Processes: VLLM::Worker_TP0..TP3  ~14720 MiB each

CSV:
NVIDIA A16, 15356 MiB, 14728/14726/14736/14724 MiB used, ~245 MiB free each, 0% util

vllm:
iiuser ... vllm serve Qwen/Qwen3.5-35B-A3B-GPTQ-Int4 --tensor-parallel-size 4 \
  --gpu-memory-utilization 0.80 --max-model-len 32768 --quantization moe_wna16 ...

free -h:
Mem: 62Gi total, 17Gi used, 44Gi available
Swap: 8Gi unused

docker stats xca-opencode:
CPU 9.41%, MEM 1.507GiB / 62.79GiB
```

</details>
