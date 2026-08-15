# Переделка магазина в сервис — план реализации

> **Для исполнителя:** шаги помечены чекбоксами. Спека:
> [`docs/superpowers/specs/2026-08-15-service-orders-redesign-design.md`](../specs/2026-08-15-service-orders-redesign-design.md)

**Цель:** магазин продаёт работу над аккаунтом покупателя, а не содержимое склада:
цена в долларах, оплата в рублях по курсу ЦБ плюс 10%, покупатель отдаёт логин с
паролем, получает токен, администратор подтверждает выполнение.

**Подход:** девять фаз, каждая оставляет дерево в рабочем состоянии. Схема меняется
одной миграцией в фазе 4 — после того как код перестал зависеть от удаляемых
таблиц, и до того как появился код, зависящий от новых.

**Стек:** Python 3.12, aiogram 3, SQLAlchemy 2 (async), Alembic, MySQL 8, Redis,
rapidfuzz, pytest.

---

## Порядок фаз и почему он такой

Удаление идёт до добавления. Если сначала завести новые поля, а потом вырезать
склад, промежуточные коммиты будут содержать обе модели сразу — и тесты в них
проверяют то, чего уже нет, вперемешку с тем, чего ещё нет.

| Фаза | Что | Дерево после фазы |
|---|---|---|
| 1 | Роли администраторов удаляются | зелёное |
| 2 | Склад и типы выдачи удаляются | зелёное, покупка временно не работает |
| 3 | Курс валют и расчёт цены | зелёное |
| 4 | Одна миграция схемы | зелёное, `check-migrations.sh` проходит |
| 5 | Новый путь заказа | зелёное, покупка работает |
| 6 | Мастер выкладки товара | зелёное |
| 7 | Fuzzy-поиск | зелёное |
| 8 | kassa.ai и настройки | зелёное |
| 9 | Мутации, демо-товары, README | зелёное |

---

## Фаза 1. Удаление ролей

**Файлы**
- Изменить: `bot/services/access.py` — матрица `PERMISSIONS` и четыре двери уходят,
  остаётся `is_admin`
- Изменить: `bot/db/models.py` — класс `AdminRole`, поле `Admin.role`
- Изменить: `bot/keyboards/admin.py:49` — `menu(is_owner)` → `menu()`
- Изменить: `bot/handlers/admin/menu.py`, `common.py`, `people.py`
- Изменить: `bot/handlers/admin/{categories,products,orders,promo,broadcast,content,stock}.py`
  — вызовы `guard(call, actor, section, door)` → `guard(call, actor)`
- Изменить: `tests/test_access.py`, `tests/factories.py`
- Изменить: `scripts/mutate.py` — мутации `access-default-deny`, `access-unknown-section`

- [ ] Переписать `tests/test_access.py`: не-администратор не проходит; администратор
      проходит; `role=None` отказ
- [ ] Прогнать — падает на отсутствии новой сигнатуры
- [ ] Переписать `access.py`: `Actor(user_id, is_admin)`, функция `allows(is_admin)`
- [ ] Пройтись по всем `guard(...)` и `is_owner`
- [ ] Прогнать тесты, коммит

## Фаза 2. Удаление склада и типов выдачи

**Файлы**
- Удалить: `bot/repo/stock.py`, `bot/handlers/admin/stock.py`,
  `bot/services/stock_input.py`, `tests/test_stock_input.py`,
  `tests/test_delivery_types_db.py`
- Изменить: `bot/db/models.py` — `StockBatch`, `StockItem`, `OrderItem`,
  `StockStatus`, `DeliveryType`, `Product.delivery_type`, `Order.delivery_type`
- Изменить: `bot/services/delivery.py` — переписывается целиком в фазе 5, здесь
  сводится к переводу заказа в `awaiting_credentials` без товара
- Изменить: `bot/services/{orders,refunds,stats}.py`, `bot/repo/catalog.py`
- Изменить: `bot/handlers/user/{catalog,purchase}.py` — раздел «Наличие», пометка
  `(нет)`, выбор количества
- Изменить: `bot/keyboards/{user,admin}.py`
- Изменить: `tests/{factories,test_orders_db,test_payments_db,test_refunds_db,test_keyboards}.py`

- [ ] Удалить файлы склада
- [ ] Вычистить модели и `MANUAL_STOCK`
- [ ] Убрать «Наличие» из меню и количество из карточки
- [ ] Убрать «Склад» из админ-меню, замену товара и брак партии из заказов
- [ ] Прогнать тесты, коммит

## Фаза 3. Курс валют и цена

**Файлы**
- Создать: `bot/services/rates.py` — получение курса ЦБ, кеш, отказ при отсутствии
- Создать: `bot/services/pricing.py` — пересчёт центов в копейки, наценка
- Создать: `tests/test_pricing.py`, `tests/test_rates.py`
- Изменить: `bot/db/models.py` — `ExchangeRate`
- Изменить: `bot/scheduler/jobs.py` — часовое задание
- Изменить: `bot/services/settings_store.py` — `price_markup_pct`

- [ ] Тест `test_pricing.py`: `$20 × 90,00 ₽ = 1800 ₽`; с наценкой 10% → `1980 ₽`;
      промокод применяется после наценки; округление половины **вверх**
      (`ROUND_HALF_UP`, как в спеке), проверенное на ничьих и с чётной, и с
      нечётной целой частью — один пример на функцию правило не удерживает
- [ ] Реализовать `pricing.py`
- [ ] Тест `test_rates.py`: разбор XML ЦБ с `Nominal=1`; запятая как разделитель;
      отсутствие USD в ответе → ошибка; пустая база → `None`
- [ ] Реализовать `rates.py` и модель `ExchangeRate`
- [ ] Часовое задание в планировщике
- [ ] Прогнать тесты, коммит

## Фаза 4. Миграция схемы

**Файлы**
- Создать: `alembic/versions/<hash>_service_orders.py`
- Изменить: `bot/db/models.py` — новые поля заказа и товара

- [ ] Добавить в модели: `Product.price_usd_cents`; `Order.token`,
      `account_login`, `account_password`, `price_usd_cents`, `rate_kop`,
      `markup_pct`; статусы `awaiting_credentials`, `in_work`
- [ ] Сгенерировать миграцию, вручную дописать: перенос `price_kop` по константе
      курса, переименование статусов, выдачу токенов зависшим заказам, удаление
      таблиц склада и `order_items`, удаление `admins.role`
- [ ] `deploy/check-migrations.sh` — накат, повтор, откат, накат, сверка
- [ ] Коммит

## Фаза 5. Новый путь заказа

**Файлы**
- Создать: `bot/services/tokens.py` — генерация токена
- Создать: `bot/services/fulfillment.py` — реквизиты, уведомление, подтверждение
- Создать: `tests/test_tokens.py`, `tests/test_fulfillment_db.py`
- Изменить: `bot/services/delivery.py`, `bot/services/payments.py`
- Изменить: `bot/handlers/user/purchase.py`, `bot/handlers/user/profile.py`
- Изменить: `bot/handlers/admin/orders.py`
- Изменить: `bot/states/{user,admin}.py`, `bot/keyboards/{user,admin}.py`

- [ ] Тест токена: алфавит без `0O1IL`; формат `XXXX-XXXX`; повтор при коллизии
- [ ] Реализовать `tokens.py`
- [ ] Тест: подтверждение оплаты выдаёт токен и ставит `awaiting_credentials`;
      повторное подтверждение не меняет токен
- [ ] Переписать `delivery.py` под новую модель
- [ ] Тест: заказ не уходит в `in_work` без обоих реквизитов
- [ ] Состояние `PurchaseSG.credentials`, разбор двух строк, отдельный запрос
      пароля при одной строке
- [ ] Уведомление администраторам с двумя кнопками; кнопка «Связаться» только при
      наличии юзернейма
- [ ] Тест идемпотентности подтверждения выполнения
- [ ] Кнопка «Отправить логин и пароль» в «Мои покупки»
- [ ] Прогнать тесты, коммит

## Фаза 6. Мастер выкладки товара

**Файлы**
- Создать: `bot/handlers/admin/product_wizard.py`
- Изменить: `bot/states/admin.py` — `ProductWizardSG`
- Изменить: `bot/handlers/admin/products.py` — кнопка «Выложить товар»
- Создать: `tests/test_product_wizard.py`

- [ ] Тест: обрыв на каждом из шести шагов не создаёт товар
- [ ] Шаги: категория → название → картинка → цена → описание → предпросмотр
- [ ] Прогнать тесты, коммит

## Фаза 7. Fuzzy-поиск

**Файлы**
- Создать: `bot/services/search.py`
- Создать: `tests/test_search.py`
- Изменить: `bot/repo/catalog.py:161`, `requirements.txt`

- [ ] Тест: опечатка `нетфликc`; перестановка слов; раскладка `Ytnabrc`;
      порог отсекает мусор; точное вхождение выше длинного совпадения
- [ ] Реализовать нормализацию и два прохода
- [ ] Прогнать тесты, коммит

## Фаза 8. kassa.ai

**Файлы**
- Создать: `bot/payments/kassa.py`
- Изменить: `bot/payments/registry.py`, `bot/config.py`, `.env.example`

- [ ] `KassaProvider` с тремя методами, каждый возбуждает `ProviderError`
- [ ] Настройки и адрес callback
- [ ] Проверка при старте: `KASSA_ENABLED=true` не даёт подняться
- [ ] Platega выключена в `.env.example`
- [ ] Коммит

## Фаза 9. Мутации, демо-товары, документация

**Файлы**
- Изменить: `scripts/mutate.py`, `scripts/seed_demo.py`, `README.md`

- [ ] Убрать три мутации склада, переписать три под новую модель
- [ ] Добавить пять: заморозка курса, реквизиты до `in_work`, идемпотентность
      подтверждения, порог поиска, уникальность токена
- [ ] `seed_demo.py` — категории и товары в долларах с картинками
- [ ] Обновить «Что умеет» и ограничения в README
- [ ] `deploy/run-tests-gate.sh`, `scripts/mutate.py`, коммит
