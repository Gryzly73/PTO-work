/**
 * ЗАМЕНА ДЛЯ src/lib/process-document.ts во фронтенде (pto-drawings).
 *
 * Вместо разбора текстового слоя через unpdf документ уходит в наш конвейер
 * (backend/service) и возвращается листами по мере готовности.
 *
 * Интерфейс модуля не меняется: наружу по-прежнему торчит processDocument(id),
 * поэтому src/app/api/documents/route.ts и .../process/route.ts править не надо.
 *
 * Что важно знать:
 *  - PDF не пересылается: сервис читает его прямо из uploads/ фронта, поэтому
 *    бэкенд должен быть запущен с PTO_ALLOWED_PDF_ROOTS на эту папку;
 *  - при перезапуске Next идущий прогон не теряется — клиент находит задачу
 *    по documentId и подхватывает прогресс;
 *  - листы, посчитанные раньше, конвейер не пересчитывает.
 *
 * Про устойчивость. Прогон документа идёт часами (лист чертежа — до 15 минут),
 * то есть клиент успевает сделать тысячи запросов. Сетевой сбой на этой
 * дистанции неизбежен: keep-alive соединение закрывается на той стороне, и
 * fetch падает с «fetch failed». Поэтому здесь есть ретраи, а одиночная
 * ошибка не имеет права уронить весь прогон — иначе бэкенд досчитывает
 * документ до конца, а интерфейс показывает ошибку и половину листов.
 */
import { getDocument, updateDocument } from "@/lib/storage";
import type { DocumentPage, DocumentRecord, ProcessingStep } from "@/types";
import path from "path";

const BACKEND_URL = (
  process.env.PTO_BACKEND_URL ?? "http://127.0.0.1:8000"
).replace(/\/+$/, "");
const POLL_MS = Number(process.env.PTO_POLL_MS ?? 3000);
// Общий секрет, если бэкенд подняли с PTO_API_TOKEN (на сервере так и надо:
// иначе очередь открыта всем, кто дотянется до порта). Пусто — заголовок не
// шлём, поведение прежнее.
const BACKEND_TOKEN = process.env.PTO_BACKEND_TOKEN ?? "";
const PAGES_PER_TICK = 40;
// Одна попытка запроса из трёх обычно проходит; сдаёмся только если сервис
// молчит подряд столько тиков, что это уже не икота, а падение.
const REQUEST_ATTEMPTS = 3;
const MAX_CONSECUTIVE_FAILURES = Number(process.env.PTO_MAX_FAILURES ?? 20);

// Так же, как persist.ts вычисляет папку загрузок.
const ROOT = process.env.DATA_ROOT || process.cwd();
const UPLOAD_DIR = path.join(ROOT, "uploads");

type BackendStatus = "queued" | "processing" | "done" | "error" | "canceled";

type BackendJob = {
  id: string;
  status: BackendStatus;
  pageCount: number;
  pagesDone: number[];
  pagesDoneCount: number;
  pagesTotal: number;
  processingPage: number | null;
  processingStep: ProcessingStep | null;
  errorMessage: string | null;
  pageErrors: Record<string, string>;
};

type BackendPage = {
  pageNumber: number;
  kind: DocumentPage["kind"];
  markdown: string;
  extractedText: string;
};

const running = new Set<string>();
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Ошибка, которую нет смысла повторять (неверный запрос, нет задачи). */
class PermanentError extends Error {}

async function api<T>(pathname: string, init?: RequestInit): Promise<T> {
  let lastError: unknown;

  for (let attempt = 1; attempt <= REQUEST_ATTEMPTS; attempt += 1) {
    try {
      const response = await fetch(`${BACKEND_URL}${pathname}`, {
        ...init,
        cache: "no-store",
        headers: BACKEND_TOKEN
          ? {
              ...((init?.headers as Record<string, string>) ?? {}),
              "X-PTO-Token": BACKEND_TOKEN,
            }
          : init?.headers,
      });

      if (response.ok) return (await response.json()) as T;

      const detail = await response.text().catch(() => "");
      const message = `Конвейер ответил ${response.status}${
        detail ? `: ${detail.slice(0, 200)}` : ""
      }`;
      // 4xx (кроме 429) — наша ошибка, повтор не поможет. 401 сюда же:
      // значит, не задан или неверен PTO_BACKEND_TOKEN.
      if (response.status < 500 && response.status !== 429) {
        throw new PermanentError(message);
      }
      lastError = new Error(message);
    } catch (error) {
      if (error instanceof PermanentError) throw error;
      lastError = error;
    }

    if (attempt < REQUEST_ATTEMPTS) await sleep(1000 * attempt);
  }

  throw lastError instanceof Error
    ? lastError
    : new Error("Конвейер недоступен");
}

async function findExistingJob(documentId: string): Promise<BackendJob | null> {
  const payload = await api<{ jobs: BackendJob[] }>(
    `/jobs?documentId=${encodeURIComponent(documentId)}`,
  );
  const jobs = payload.jobs ?? [];
  if (jobs.length === 0) return null;
  // Живая задача важнее завершённой: после перезапуска Next подхватываем её.
  return (
    jobs.find((job) => job.status === "processing" || job.status === "queued") ??
    jobs[jobs.length - 1]
  );
}

async function createJob(document: DocumentRecord): Promise<BackendJob> {
  // Файл не пересылается: бэкенд стоит рядом и читает его из той же папки
  // uploads по пути. Поэтому сервису нужен PTO_ALLOWED_PDF_ROOTS на неё.
  return api<BackendJob>("/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      path: path.join(UPLOAD_DIR, document.storedName),
      originalName: document.originalName,
      projectId: document.projectId,
      documentId: document.id,
    }),
  });
}

async function fetchPages(
  jobId: string,
  numbers: number[],
): Promise<BackendPage[]> {
  if (numbers.length === 0) return [];
  const payload = await api<{ pages: BackendPage[] }>(
    `/jobs/${jobId}/pages?pages=${numbers.join(",")}`,
  );
  return payload.pages ?? [];
}

function mapStatus(status: BackendStatus): DocumentRecord["status"] {
  // У фронта нет отдельного «отменён» — показываем как ошибку с пояснением.
  if (status === "canceled") return "error";
  return status;
}

function stepFor(job: BackendJob): ProcessingStep | null {
  if (job.status === "done") return "done";
  if (job.status === "queued") return "queued";
  if (job.status === "processing") return job.processingStep ?? "text";
  return null;
}

export async function processDocument(id: string) {
  if (running.has(id)) return;
  running.add(id);

  try {
    const document = await getDocument(id);
    if (!document) return;

    await updateDocument(id, {
      status: "processing",
      processingStep: "queued",
      processingPage: null,
      errorMessage: null,
    });

    const job = (await findExistingJob(id)) ?? (await createJob(document));
    const pages = new Map<number, DocumentPage>(
      document.pages.map((page) => [page.pageNumber, page]),
    );
    // storage.ts переписывает весь db.json целиком, поэтому пишем только когда
    // реально что-то изменилось. Иначе на длинном прогоне мы бы перезаписывали
    // базу каждые POLL_MS без причины.
    let lastSignature = "";
    let failures = 0;

    for (;;) {
      let current: BackendJob;
      try {
        current = await api<BackendJob>(`/jobs/${job.id}`);

        const missing = current.pagesDone
          .filter((pageNumber) => !pages.has(pageNumber))
          .slice(0, PAGES_PER_TICK);
        for (const page of await fetchPages(job.id, missing)) {
          pages.set(page.pageNumber, {
            pageNumber: page.pageNumber,
            kind: page.kind,
            markdown: page.markdown,
            extractedText: page.extractedText,
          });
        }
        failures = 0;
      } catch (error) {
        if (error instanceof PermanentError) throw error;
        failures += 1;
        // Бэкенд считает документ часами и переживает сетевые сбои. Клиент
        // обязан вести себя так же, иначе прогон продолжится, а интерфейс
        // покажет ошибку и половину листов.
        if (failures >= MAX_CONSECUTIVE_FAILURES) throw error;
        console.warn(
          `[pto] конвейер не ответил (${failures}/${MAX_CONSECUTIVE_FAILURES}), повтор:`,
          error instanceof Error ? error.message : error,
        );
        await sleep(POLL_MS);
        continue;
      }

      const finished =
        current.status === "done" ||
        current.status === "error" ||
        current.status === "canceled";
      const failedPages = Object.keys(current.pageErrors ?? {}).length;

      const signature = [
        current.status,
        stepFor(current),
        current.processingPage,
        pages.size,
        failedPages,
      ].join("|");

      if (signature !== lastSignature) {
        lastSignature = signature;
        await updateDocument(id, {
          status: mapStatus(current.status),
          processingStep: stepFor(current),
          processingPage: current.processingPage,
          pageCount: current.pageCount || document.pageCount,
          pages: [...pages.values()].sort((a, b) => a.pageNumber - b.pageNumber),
          errorMessage:
            current.errorMessage ??
            (finished && failedPages
              ? `Не удалось обработать листов: ${failedPages}`
              : null),
        });
      }

      if (finished) return;
      await sleep(POLL_MS);
    }
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Не удалось обработать файл";
    // Листы, которые успели прийти, уже лежат в базе — статус ошибки их не
    // стирает. Повторный запуск обработки заберёт остальное с бэкенда, ничего
    // не пересчитывая.
    await updateDocument(id, {
      status: "error",
      processingStep: null,
      processingPage: null,
      errorMessage: `${message}. Нажмите «Обработать заново» — готовые листы подтянутся без пересчёта.`,
    });
  } finally {
    running.delete(id);
  }
}
