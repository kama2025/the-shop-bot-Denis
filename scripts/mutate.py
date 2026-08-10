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
        name="delivery-idempotency",
        path="bot/services/delivery.py",
        anchor="    if order.status == OrderStatus.DELIVERED:\n"
        "        return DeliveryResult(\n"
        "            ok=True, items=await items_of(session, order.id), already_delivered=True\n"
        "        )",
        replacement="    if False:  # МУТАЦИЯ\n"
        "        return DeliveryResult(\n"
        "            ok=True, items=await items_of(session, order.id), already_delivered=True\n"
        "        )",
        tests=[
            "tests/test_orders_db.py::test_delivery_is_idempotent",
            "tests/test_payments_db.py::test_confirmed_delivers_once",
        ],
        breaks="снята отсечка «заказ уже выдан»",
        guards="один заказ — не более одной выдачи",
    ),
    Mutation(
        name="access-default-deny",
        path="bot/services/access.py",
        anchor="    return role in allowed",
        replacement="    return True  # МУТАЦИЯ",
        tests=["tests/test_access.py"],
        breaks="проверка роли всегда отвечает «разрешено»",
        guards="умолчание в правах — отказ",
    ),
    Mutation(
        name="access-unknown-section",
        path="bot/services/access.py",
        anchor="    doors = PERMISSIONS.get(section)\n"
        "    if doors is None:\n"
        "        return False",
        replacement="    doors = PERMISSIONS.get(section)\n"
        "    if doors is None:\n"
        "        return True  # МУТАЦИЯ",
        tests=["tests/test_access.py::test_unknown_section_is_denied"],
        breaks="неизвестный раздел становится разрешённым",
        guards="раздела нет в таблице — значит закрыт",
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
        tests=["tests/test_payments_db.py::test_amount_mismatch_blocks_delivery"],
        breaks="перестала сверяться сумма платежа с суммой заказа",
        guards="товар не выдаётся, если заплатили не столько",
    ),
    Mutation(
        name="payment-payload-check",
        path="bot/services/payments.py",
        anchor='    if payload is not None and str(payload).strip() '
        'and str(payload).strip() != str(order.id):',
        replacement="    if False:  # МУТАЦИЯ",
        tests=["tests/test_payments_db.py::test_foreign_payload_blocks_delivery"],
        breaks="перестал сверяться payload платежа с номером заказа",
        guards="чужая оплата не закрывает наш заказ",
    ),
    Mutation(
        name="stock-partial-reserve",
        path="bot/repo/stock.py",
        # Якорь захватывает и следующую строку: точно такая же проверка есть
        # в `take_available`, и без уточнения поломка встаёт не туда.
        anchor="    if len(items) < qty:\n"
        "        return []\n"
        "\n"
        "    for item in items:\n"
        "        item.status = StockStatus.RESERVED",
        replacement="    if False:  # МУТАЦИЯ\n"
        "        return []\n"
        "\n"
        "    for item in items:\n"
        "        item.status = StockStatus.RESERVED",
        tests=[
            "tests/test_orders_db.py::test_reserve_never_leaves_partial_hold",
            "tests/test_orders_db.py::test_two_buyers_never_get_the_same_item",
        ],
        breaks="разрешён частичный резерв склада",
        guards="двум покупателям не достаётся одна позиция",
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
        name="manual-awaits-admin",
        path="bot/services/delivery.py",
        anchor="    if order.delivery_type == DeliveryType.MANUAL:\n"
        "        return await _await_manual(session, order)",
        replacement="    if False:  # МУТАЦИЯ\n"
        "        return await _await_manual(session, order)",
        tests=["tests/test_delivery_types_db.py"],
        breaks="товар с ручной выдачей перестал попадать в очередь к админу",
        guards="оплаченный заказ с ручной выдачей ждёт администратора, а не считается выданным",
    ),
    Mutation(
        name="manual-always-available",
        path="bot/repo/stock.py",
        anchor="    if kind == DeliveryType.MANUAL:\n        return MANUAL_STOCK",
        replacement="    if False:  # МУТАЦИЯ\n        return MANUAL_STOCK",
        tests=["tests/test_delivery_types_db.py::test_manual_product_is_always_buyable"],
        breaks="товар с ручной выдачей стал «нет в наличии»",
        guards="товар без склада всё равно можно купить",
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
    Mutation(
        name="balance-negative-guard",
        path="bot/repo/balance.py",
        anchor="    if new_balance < 0 and not allow_negative:",
        replacement="    if False:  # МУТАЦИЯ",
        tests=[
            "tests/test_orders_db.py::test_balance_cannot_go_negative",
            "tests/test_orders_db.py::test_parallel_spending_cannot_overdraw",
        ],
        breaks="снята защита от ухода баланса в минус",
        guards="с баланса нельзя списать больше, чем на нём есть",
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
