#!/usr/bin/env bash
#
# Тест-гейт с базовой линией.
#
# Блокирует не любые падения, а **новые относительно `test-baseline.txt`**.
# Гейт «упал любой тест → выкат запрещён» на второй день отключают, и
# тестирования не остаётся вовсе.
#
# Коды возврата:
#   0 — новых падений нет;
#   1 — есть новые падения (или линия выросла без разрешения);
#   3 — ПРОГОН НЕ СОСТОЯЛСЯ.
#
# Про код 3 отдельно. Типовой гейт устроен как `pytest | grep '^FAILED'`.
# Если pytest не запустился, grep пуст, и гейт рапортует «0 падений». Это не
# «чисто», это «не проверяли». Поэтому у «проверка не состоялась» свой код, и
# рядом с нулём всегда печатается счётчик того, что вообще проверялось.
#
# Использование:
#   deploy/run-tests-gate.sh
#   deploy/run-tests-gate.sh --update                 # пересобрать линию
#   deploy/run-tests-gate.sh --force-grow="причина"   # разрешить рост линии
#
set -uo pipefail

cd "$(dirname "$0")/.." || exit 3
ROOT="$PWD"

RC_NOT_RUN=3
PY="${PY:-$ROOT/.venv/bin/python}"
BASELINE="$ROOT/deploy/test-baseline.txt"
REPORT="$ROOT/deploy/.last-test-run.txt"

MODE="check"
GROW_REASON=""
for arg in "$@"; do
  case "$arg" in
    --update) MODE="update" ;;
    --force-grow=*) GROW_REASON="${arg#*=}" ;;
    *) echo "Неизвестный аргумент: $arg" >&2; exit $RC_NOT_RUN ;;
  esac
done

say()  { printf '%s\n' "$*"; }
fail() { printf '✗ %s\n' "$*" >&2; }

# --- 1. Может ли прогон вообще состояться ----------------------------------

[ -x "$PY" ] || { fail "ГЕЙТ НЕ ЗАПУЩЕН: нет интерпретатора '$PY'"; exit $RC_NOT_RUN; }

if ! "$PY" -c 'import pytest' >/dev/null 2>&1; then
  fail "ГЕЙТ НЕ ЗАПУЩЕН: '$PY' не может импортировать pytest"
  exit $RC_NOT_RUN
fi

if ! "$PY" -c 'import bot.db.models' >/dev/null 2>&1; then
  fail "ГЕЙТ НЕ ЗАПУЩЕН: не импортируется сам проект — проверьте зависимости"
  exit $RC_NOT_RUN
fi

# --- 2. Прогон --------------------------------------------------------------

say "Прогон тестов…"
# Без -q: в pytest.ini уже стоит -q, а второй делает вывод «-qq» и убирает
# итоговую строку «N passed». Гейт тогда насчитывает ноль тестов и честно
# сообщает «прогон не состоялся» — что и произошло при первом запуске.
"$PY" -m pytest --tb=short -rf > "$REPORT" 2>&1
PYTEST_RC=$?

# Коды pytest: 0 — всё прошло, 1 — есть падения, 5 — не собрано ни одного
# теста. Остальное (2 — прерывание, 3 — внутренняя ошибка, 4 — ошибка
# использования) означает, что прогон не состоялся.
case "$PYTEST_RC" in
  0|1) : ;;
  5)
    fail "ГЕЙТ НЕ ЗАПУЩЕН: pytest не собрал ни одного теста"
    tail -20 "$REPORT" >&2
    exit $RC_NOT_RUN
    ;;
  *)
    fail "ГЕЙТ НЕ ЗАПУЩЕН: pytest завершился с кодом $PYTEST_RC"
    tail -30 "$REPORT" >&2
    exit $RC_NOT_RUN
    ;;
esac

# --- 3. Счётчики: ноль сам по себе не результат -----------------------------

SUMMARY=$(grep -E '(passed|failed|skipped|error)' "$REPORT" | tail -1)

# Через grep -o, а не через `sed 's/.*[^0-9]([0-9]+) passed/'`: итоговая строка
# бывает и «176 passed in 12s», и «1 failed, 175 passed». Во втором случае
# шаблон с обязательным нецифровым символом перед числом ещё срабатывает, а в
# первом — нет, потому что строка начинается с самого числа. Гейт тогда
# насчитывает ноль и рапортует «не проверяли» на полностью зелёном прогоне.
count_of() { grep -oE "[0-9]+ $1" <<<"$SUMMARY" | grep -oE '^[0-9]+' | head -1; }

PASSED=$(count_of passed)
FAILED_N=$(count_of failed)
SKIPPED=$(count_of skipped)
PASSED=${PASSED:-0}
FAILED_N=${FAILED_N:-0}
SKIPPED=${SKIPPED:-0}
TOTAL=$((PASSED + FAILED_N + SKIPPED))

say "Прошло: $PASSED · упало: $FAILED_N · пропущено: $SKIPPED · всего: $TOTAL"

if [ "$TOTAL" -eq 0 ]; then
  fail "ГЕЙТ НЕ ЗАПУЩЕН: ни один тест не выполнился"
  exit $RC_NOT_RUN
fi

if [ "$SKIPPED" -gt 0 ]; then
  say "⚠️  Пропущено $SKIPPED тестов. Пропуск — это непроверенное, а не пройденное."
  grep -E '^SKIPPED' "$REPORT" | head -5 | sed 's/^/    /'
fi

# --- 4. Сравнение с базовой линией -----------------------------------------

grep -E '^FAILED ' "$REPORT" | sed -E 's/^FAILED ([^ ]+).*/\1/' | sort -u > "$ROOT/deploy/.current-failures.txt"
CURRENT="$ROOT/deploy/.current-failures.txt"

if [ "$MODE" = "update" ]; then
  {
    echo "# Известные падения. У каждой строки обязан быть комментарий: ПОЧЕМУ она здесь."
    echo "# Линия должна сокращаться. Рост допустим только с --force-grow=\"причина\"."
    echo "# Пересобрано: $(date '+%Y-%m-%d %H:%M')"
    cat "$CURRENT"
  } > "$BASELINE"
  say "Базовая линия пересобрана: $(wc -l < "$CURRENT" | tr -d ' ') падений."
  say "⚠️  Внесите к каждой строке комментарий, почему она там."
  say "    --update нельзя применять к падению, которое не разобрано: среди"
  say "    «протухших» падений регулярно прячутся настоящие дыры."
  exit 0
fi

[ -f "$BASELINE" ] || : > "$BASELINE"
grep -vE '^\s*(#|$)' "$BASELINE" | sort -u > "$ROOT/deploy/.baseline-clean.txt"
BASE="$ROOT/deploy/.baseline-clean.txt"

NEW=$(comm -23 "$CURRENT" "$BASE")
FIXED=$(comm -13 "$CURRENT" "$BASE")

BASE_N=$(wc -l < "$BASE" | tr -d ' ')
CUR_N=$(wc -l < "$CURRENT" | tr -d ' ')

if [ -n "$FIXED" ]; then
  say ""
  say "✅ Починено с прошлого раза:"
  printf '%s\n' "$FIXED" | sed 's/^/    /'
  say "   Уберите эти строки из deploy/test-baseline.txt — линия должна сокращаться."
fi

if [ -n "$NEW" ]; then
  say ""
  fail "НОВЫЕ падения ($(printf '%s\n' "$NEW" | wc -l | tr -d ' ')):"
  printf '%s\n' "$NEW" | sed 's/^/    /' >&2
  say ""
  say "Подробности: deploy/.last-test-run.txt"
  exit 1
fi

if [ "$CUR_N" -gt "$BASE_N" ] && [ -z "$GROW_REASON" ]; then
  fail "Базовая линия выросла: было $BASE_N, стало $CUR_N. Долг узаконивать нельзя."
  fail "Если рост осознан: --force-grow=\"причина\""
  exit 1
fi

if [ -n "$GROW_REASON" ]; then
  say "⚠️  Рост линии разрешён вручную. Причина: $GROW_REASON"
fi

say ""
say "✅ Новых падений нет. Известных: $CUR_N (линия: $BASE_N). Проверено тестов: $TOTAL."
exit 0
