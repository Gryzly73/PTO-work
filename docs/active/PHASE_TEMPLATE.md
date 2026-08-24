# PHASE Dn: \<короткое имя\>

**Basket:** [P01_HARNESS.md](./P01_HARNESS.md) · **Dn**  
**Источник:** P01 / ТЗ §…  
**Статус:** pending  
**Зависит от:** D(n-1)  
**Код при составлении scope:** не менялся

---

## Цель

Одно предложение: какой контракт ТЗ закрываем.

## In scope

- файлы и действия списком

## Out of scope

- что сознательно не делаем в этом витке

## Acceptance

- [ ] …

## Verify

```powershell
cd PTO-work
python -m pytest -q
```

## Harness

**Ask:** риски / A/B, без правок.  
**Agent:** реализуй по `@docs/active/PHASE_Dn_….md` — только in scope; не коммитить без запроса; не звать HF.

## Checkpoint

| Поле | Значение |
|------|----------|
| Done | |
| Verify | |
| PR | |
| Basket P01 | Dn → … |
| Долг / replan | |
