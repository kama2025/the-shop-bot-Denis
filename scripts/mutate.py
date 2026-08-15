#!/usr/bin/env python3
"""Мутационная проверка: ломаем защищаемое и убеждаемся, что тест краснеет.

Зелёный тест — не доказательство. Доказательство — тест, который **видели
красным** на сломанном коде.

Четыре решения, каждое куплено чужой потерей.

1. **Отказ на грязном дереве.** Прогон не начинается, если в ломаемом файле
   есть незакоммиченные правки. Возврат через `git checkout` дважды за сутки
   стирал чужую незавершённую работу — причём один из авторов сам написал
   предупреждение об этом и всё равно запустил. Просить внимательности
   бесполезно: инструмент, который умеет разрушать, обязан запрещать запуск.
2. **Возврат из памяти, а не из git.** Исходные байты читаются до правки и
   пишутся обратно в `finally`. На чистом дереве два способа неотличимы —
   ровно до первого грязного.
3. **Печать исходного состояния.** Коммит и сколько раз найден якорь каждой
   мутации. Ложным доказательство делает не поломка, а уверенность, что «до»
   было таким, как мы думаем.
4. **Отдельный код для «прогон не состоялся».** Якорь, найденный ноль раз, —
   поломка не встала вовсе; найденный дважды — встала не туда.

Коды возврата:
    0 — все мутации убиты (тесты покраснели);
    1 — есть выжившие;
    3 — ПРОГОН НЕ СОСТОЯЛСЯ.

Использование:
    scripts/mutate.py              все мутации
    scripts/mutate.py --list       список
    scripts/mutate.py access       только мутации, чьё имя содержит «access»
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "bin" / "python"

RC_OK = 0
RC_SURVIVED = 1
RC_NOT_RUN = 3


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    anchor: str
    replacement: str
    tests: list[str]
    breaks: str
    guards: str = ""
    _unused: tuple = field(default=(), repr=False)


MUTATIONS: list[Mutation] = [
    Mutation(
        name="delivery-requires-payment",
        path="bot/services/delivery.py",
        anchor="    if order.status != OrderStatus.PAID:\n"
        "        return StepResult(ok=False, order=order)",
        replacement="    if False:  # МУТАЦИЯ\n"
        "        return StepResult(ok=False, order=order)",
        tests=["tests/test_payments_db.py", "tests/test_fulfillment_db.py"],
        breaks="снята проверка «заказ оплачен» перед выдачей токена",
        guards="токен и работа достаются только оплаченному заказу",
    ),
    Mutation(
        name="token-issued-once",
        path="bot/services/delivery.py",
        anchor="    if not order.token:",
        replacement="    if True:  # МУТАЦИЯ",
        tests=["tests/test_payments_db.py"],
        breaks="токен перевыдаётся при каждом подтверждении оплаты",
        guards="покупателю не меняют номер заказа задним числом",
    ),
    Mutation(
        name="credentials-required",
        path="bot/services/delivery.py",
        anchor="    if not login or not password:\n"
        "        return StepResult(ok=False, order=order)",
        replacement="    if False:  # МУТАЦИЯ\n"
        "        return StepResult(ok=False, order=order)",
        tests=["tests/test_fulfillment_db.py"],
        breaks="заказ уходит в работу с пустым логином или пустым паролем",
        guards="администратор не получает заказ, по которому нельзя работать",
    ),
    Mutation(
        name="confirm-done-idempotency",
        path="bot/services/delivery.py",
        anchor="    if order.status == OrderStatus.DELIVERED:\n"
        "        return StepResult(ok=True, order=order, repeated=True)\n"
        "\n"
        "    if order.status != OrderStatus.IN_WORK:",
        replacement="    if False:  # МУТАЦИЯ\n"
        "        return StepResult(ok=True, order=order, repeated=True)\n"
        "\n"
        "    if order.status != OrderStatus.IN_WORK:",
        tests=["tests/test_fulfillment_db.py"],
        breaks="повторное подтверждение выполнения проходит как первое",
        guards="покупатель не получает второе «заказ выполнен», исполнитель в журнале не подменяется",
    ),
    Mutation(
        name="confirm-order-row-lock",
        path="bot/services/payments.py",
        anchor="    order = await orders_repo.get_for_update(session, order_id)\n"
        "    if order is None:\n"
        "        return ConfirmResult(Outcome.NOT_FOUND)\n"
        "\n"
        "    # 1. Оплаченные заказы",
        replacement="    order = await orders_repo.get(session, order_id)  # МУТАЦИЯ\n"
        "    if order is None:\n"
        "        return ConfirmResult(Outcome.NOT_FOUND)\n"
        "\n"
        "    # 1. Оплаченные заказы",
        tests=["tests/test_payments_db.py"],
        breaks="подтверждение оплаты читает заказ без блокировки строки",
        guards="два одновременных подтверждения не проводят платёж дважды",
    ),
    Mutation(
        name="rate-frozen-in-order",
        path="bot/services/orders.py",
        anchor="        rate_kop=calc.rate_kop,",
        replacement="        rate_kop=0,  # МУТАЦИЯ",
        tests=["tests/test_orders_db.py"],
        breaks="курс не сохраняется в заказе",
        guards="сумма оплаченного заказа не зависит от того, как потом двинулся курс",
    ),
    Mutation(
        name="rate-required-to-sell",
        path="bot/services/orders.py",
        anchor="    if rate_kop <= 0:\n"
        "        raise RateUnavailable",
        replacement="    if False:  # МУТАЦИЯ\n"
        "        raise RateUnavailable",
        tests=["tests/test_orders_db.py"],
        breaks="товар продаётся при отсутствующем курсе",
        guards="без курса магазин отказывается продавать, а не считает по нулю",
    ),
    Mutation(
        name="search-threshold",
        path="bot/services/search.py",
        anchor="DEFAULT_THRESHOLD = 60",
        replacement="DEFAULT_THRESHOLD = 0  # МУТАЦИЯ",
        tests=["tests/test_search.py"],
        breaks="снят порог похожести в поиске",
        guards="поиск не возвращает весь каталог на любой запрос",
    ),
    Mutation(
        name="search-one-word-is-not-a-match",
        path="bot/services/search.py",
        anchor="        if len(token) >= _SIGNIFICANT_TOKEN and best < DEFAULT_THRESHOLD:\n"
        "            return 0.0",
        replacement="        if False:  # МУТАЦИЯ\n"
        "            return 0.0",
        tests=["tests/test_search.py::test_one_shared_word_is_not_a_match"],
        breaks="одно общее слово запроса вытаскивает чужой товар",
        guards="«премиум нетфликс» не находит Spotify Premium",
    ),
    Mutation(
        name="category-accent-fallback",
        path="bot/db/models.py",
        anchor="        return value if value in cls.ALL else cls.DEFAULT",
        replacement="        return value  # МУТАЦИЯ",
        tests=["tests/test_keyboards.py", "tests/test_catalog_accent.py"],
        breaks="цвет из базы уходит в кнопку без проверки",
        guards="мусор в поле цвета не роняет клавиатуру целиком",
    ),
    Mutation(
        name="refund-not-twice",
        path="bot/services/refunds.py",
        anchor="    if order.status == OrderStatus.REFUNDED:\n"
        '        return RefundResult(False, detail="Возврат по этому заказу уже оформлен")',
        replacement="    if False:  # МУТАЦИЯ\n"
        '        return RefundResult(False, detail="Возврат по этому заказу уже оформлен")',
        tests=["tests/test_refunds_db.py"],
        breaks="возврат по одному заказу проходит дважды",
        guards="деньги не возвращаются покупателю по два раза",
    ),
    Mutation(
        name="admin-header-fields",
        path="bot/handlers/admin/menu.py",
        # Ровно тот баг, что был в бою: шапка обращается к полю, которого у
        # снимка нет. Переименовать поле в самом dataclass недостаточно —
        # присваивание в collect создаст атрибут на лету, и чтение уцелеет.
        anchor='    if snapshot.orders_in_work:\n'
        '        lines.append(f"🛠 В работе: {snapshot.orders_in_work}")',
        replacement='    if True:  # МУТАЦИЯ\n'
        '        lines.append(f"📦 Свободных позиций: {snapshot.stock_available}")',
        tests=["tests/test_stats_db.py"],
        breaks="шапка админки читает поле, удалённое из снимка",
        guards="админ-панель открывается, а не падает молча в журнал",
    ),
    Mutation(
        name="access-default-deny",
        path="bot/services/access.py",
        anchor="    return is_admin is True",
        replacement="    return True  # МУТАЦИЯ",
        tests=["tests/test_access.py"],
        breaks="проверка прав всегда отвечает «разрешено»",
        guards="умолчание в правах — отказ",
    ),
    Mutation(
        name="access-truthy-is-not-admin",
        path="bot/services/access.py",
        anchor="    return is_admin is True",
        replacement="    return bool(is_admin)  # МУТАЦИЯ",
        tests=["tests/test_access.py::test_truthy_but_not_true_denied"],
        breaks="администратором становится любое истинное значение",
        guards="признак администратора — именно True, а не «что-то непустое»",
    ),
    Mutation(
        name="actor-defaults-to-admin",
        path="bot/services/access.py",
        anchor="    is_admin: bool = False",
        replacement="    is_admin: bool = True  # МУТАЦИЯ",
        tests=["tests/test_access.py::test_actor_defaults_to_denied"],
        breaks="актор без явного признака считается администратором",
        guards="умолчание у актора закрывает, а не открывает",
    ),
    Mutation(
        name="promo-usage-limit",
        path="bot/repo/promo.py",
        anchor="                PromoCode.used_count < PromoCode.usage_limit,",
        replacement="                PromoCode.used_count >= 0,  # МУТАЦИЯ",
        tests=["tests/test_orders_db.py::test_promo_last_use_goes_to_one_order_only"],
        breaks="снято условие «лимит ещё не исчерпан» из атомарного UPDATE",
        guards="последнее использование промокода достаётся ровно одному заказу",
    ),
    Mutation(
        name="payment-amount-check",
        path="bot/services/payments.py",
        anchor="    if int(amount_kop) != int(order.total_kop):",
        replacement="    if False:  # МУТАЦИЯ",
        tests=["tests/test_payments_db.py::test_amount_mismatch_blocks_credentials_request"],
        breaks="перестала сверяться сумма платежа с суммой заказа",
        guards="товар не выдаётся, если заплатили не столько",
    ),
    Mutation(
        name="payment-payload-check",
        path="bot/services/payments.py",
        anchor='    if payload is not None and str(payload).strip() '
        'and str(payload).strip() != str(order.id):',
        replacement="    if False:  # МУТАЦИЯ",
        tests=["tests/test_payments_db.py::test_foreign_payload_blocks_credentials_request"],
        breaks="перестал сверяться payload платежа с номером заказа",
        guards="чужая оплата не закрывает наш заказ",
    ),
    Mutation(
        name="subscription-membership",
        path="bot/services/subscription.py",
        anchor='            if getattr(member, "status", None) not in MEMBER_STATUSES:',
        replacement="            if False:  # МУТАЦИЯ",
        tests=["tests/test_subscription_db.py::test_unsubscribed_statuses"],
        breaks="проверка статуса участника канала обесценена",
        guards="неподписанный не попадает в магазин",
    ),
    Mutation(
        name="button-style-guard",
        path="bot/keyboards/theme.py",
        anchor="SECONDARY = DEFAULT",
        replacement='SECONDARY = "secondary"  # МУТАЦИЯ',
        tests=["tests/test_keyboards.py"],
        breaks="возвращён стиль кнопки, который Telegram не принимает",
        guards="ни одна клавиатура не собирается со стилем, отвергаемым Telegram",
    ),
    Mutation(
        name="render-send-before-delete",
        path="bot/utils/render.py",
        anchor="    sent = await _send(message, text, reply_markup, photo)\n"
        "    try:\n"
        "        await message.delete()",
        replacement="    try:  # МУТАЦИЯ\n"
        "        await message.delete()\n"
        "    except TelegramBadRequest:\n"
        "        pass\n"
        "    sent = await _send(message, text, reply_markup, photo)\n"
        "    try:\n"
        "        pass",
        tests=["tests/test_render.py"],
        breaks="старое сообщение удаляется раньше, чем отправлено новое",
        guards="экран не исчезает у покупателя, если отправка нового упала",
    ),

]


def die_not_run(message: str) -> None:
    print(f"✗ ПРОГОН НЕ СОСТОЯЛСЯ: {message}", file=sys.stderr)
    sys.exit(RC_NOT_RUN)


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def ensure_clean(paths: set[str]) -> None:
    """Запрещает запуск, если в ломаемых файлах есть незакоммиченные правки."""
    inside = git("rev-parse", "--is-inside-work-tree")
    if inside != "true":
        die_not_run(
            "каталог не под git. Возврат файла после мутации проверить нечем — "
            "сначала сделайте git init и коммит."
        )

    dirty = {
        line[3:].strip()
        for line in git("status", "--porcelain").splitlines()
        if line.strip()
    }
    collisions = sorted(paths & dirty)
    if collisions:
        print("✗ ПРОГОН НЕ СОСТОЯЛСЯ: в ломаемых файлах есть незакоммиченные правки:",
              file=sys.stderr)
        for path in collisions:
            print(f"    {path}", file=sys.stderr)
        print(
            "\n  Инструмент умеет разрушать, поэтому запуск запрещён.\n"
            "  Закоммитьте или спрячьте правки (git stash) и повторите.",
            file=sys.stderr,
        )
        sys.exit(RC_NOT_RUN)


def run_tests(tests: list[str]) -> tuple[bool, str]:
    """Возвращает (тесты_прошли, хвост_вывода)."""
    result = subprocess.run(
        [str(PYTHON), "-m", "pytest", "--tb=line", "-x", *tests],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-4:])
    if result.returncode not in (0, 1):
        die_not_run(f"pytest вернул код {result.returncode} на {tests}\n{tail}")
    return result.returncode == 0, tail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("filter", nargs="?", default="", help="подстрока имени мутации")
    parser.add_argument("--list", action="store_true", help="показать список и выйти")
    args = parser.parse_args()

    selected = [m for m in MUTATIONS if args.filter in m.name]
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name:26} — защищает: {mutation.guards}")
        return RC_OK
    if not selected:
        die_not_run(f"под фильтр «{args.filter}» не попала ни одна мутация")

    if not PYTHON.exists():
        die_not_run(f"нет интерпретатора {PYTHON}")

    ensure_clean({m.path for m in selected})

    commit = git("rev-parse", "--short", "HEAD") or "(без коммитов)"
    branch = git("rev-parse", "--abbrev-ref", "HEAD") or "(нет ветки)"
    print("Исходное состояние")
    print(f"  ветка:  {branch}")
    print(f"  коммит: {commit}")
    print(f"  мутаций к проверке: {len(selected)}")
    print()

    # Сперва убеждаемся, что на неповреждённом коде тесты зелёные. Иначе
    # «покраснело» ничего не докажет — оно и так было красным.
    print("── холостой прогон на неповреждённом коде ──")
    for mutation in selected:
        passed, tail = run_tests(mutation.tests)
        if not passed:
            die_not_run(
                f"тесты мутации «{mutation.name}» красные ДО поломки:\n{tail}\n"
                "  Красное «после» ничего не докажет, пока красное «до»."
            )
    print("✓ все выбранные тесты зелёные до мутаций\n")

    killed: list[str] = []
    survived: list[tuple[str, str]] = []

    for mutation in selected:
        target = ROOT / mutation.path
        original = target.read_bytes()
        source = original.decode("utf-8")

        found = source.count(mutation.anchor)
        print(f"── {mutation.name} ──")
        print(f"  файл:   {mutation.path}")
        print(f"  якорь найден раз: {found}")
        if found != 1:
            die_not_run(
                f"якорь мутации «{mutation.name}» найден {found} раз(а).\n"
                "  Ноль — поломка не встала вовсе; больше одного — встала не туда.\n"
                "  Обновите якорь в scripts/mutate.py под текущий код."
            )
        print(f"  ломаем: {mutation.breaks}")

        try:
            target.write_text(source.replace(mutation.anchor, mutation.replacement), "utf-8")
            passed, tail = run_tests(mutation.tests)
        finally:
            # Возврат из памяти, а не через git: на грязном дереве git-возврат
            # уносит чужую работу.
            target.write_bytes(original)

        if passed:
            survived.append((mutation.name, mutation.guards))
            print("  ✗ ВЫЖИЛА — тесты остались зелёными на сломанном коде\n")
        else:
            killed.append(mutation.name)
            print("  ✓ убита — тесты покраснели\n")

    print("─" * 60)
    print(f"Убито: {len(killed)} из {len(selected)}")

    if survived:
        print("\n✗ Выжившие мутации:")
        for name, guards in survived:
            print(f"    {name} — никто не заметил, что сломано: {guards}")
        print(
            "\n  Выжившая мутация — не повод объявить её эквивалентной.\n"
            "  Сперва спросите: не зависит ли поведение от случайности или\n"
            "  порядка тестов. И помните: выжившая мутация чаще обвиняет ТЕСТ,\n"
            "  а не код — типично тест зовёт проверяемую функцию напрямую вместо\n"
            "  прохождения боевого пути."
        )
        return RC_SURVIVED

    print("\n✅ Все мутации убиты: защиты действительно проверяются тестами.")
    return RC_OK


if __name__ == "__main__":
    sys.exit(main())
