#!/usr/bin/env bash
#
# Проверка миграций на настоящей MySQL.
#
# Цикл: накат → повтор (обязан быть no-op) → откат → повторный накат → сверка
# снимков схемы. Сверяются полные определения таблиц: типы всех колонок,
# индексы, внешние ключи и коллации, а не их количество. Порог «по сумме»
# («должно быть 26 внешних ключей») пропускает случай «снял один, добавил
# другой в другом месте».
#
# Коды возврата:
#   0 — схема сошлась;
#   1 — схема разошлась или миграция упала;
#   3 — ПРОГОН НЕ СОСТОЯЛСЯ (нет окружения, базы, alembic). Это не «чисто»,
#       это «не проверяли», и код обязан отличаться и от успеха, и от провала.
#
set -uo pipefail

cd "$(dirname "$0")/.." || exit 3
ROOT="$PWD"

RC_NOT_RUN=3
PY="${PY:-$ROOT/.venv/bin/python}"
ALEMBIC="${ALEMBIC:-$ROOT/.venv/bin/alembic}"
CONTAINER="${SHOPBOT_MYSQL_CONTAINER:-shopbot-mysql}"

say()  { printf '%s\n' "$*"; }
fail() { printf '✗ %s\n' "$*" >&2; }

# --- окружение: любая нехватка = «прогон не состоялся» ----------------------

[ -f "$ROOT/.env" ] || { fail "ГЕЙТ НЕ ЗАПУЩЕН: нет .env"; exit $RC_NOT_RUN; }

set -a
# shellcheck disable=SC1091
. "$ROOT/.env"
set +a

: "${DB_HOST:?}" "${DB_PORT:?}" "${DB_USER:?}" "${DB_NAME:?}"

[ -x "$ALEMBIC" ] || { fail "ГЕЙТ НЕ ЗАПУЩЕН: нет $ALEMBIC"; exit $RC_NOT_RUN; }
"$PY" -c 'import sqlalchemy, alembic, pymysql' 2>/dev/null || {
  fail "ГЕЙТ НЕ ЗАПУЩЕН: '$PY' не может импортировать sqlalchemy/alembic/pymysql"
  exit $RC_NOT_RUN
}

# Как ходить в СУБД: через контейнер (разработка) или локальным клиентом (прод).
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
  MODE="docker"
  mysql_do()   { docker exec -i "$CONTAINER" mysql -u"$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" "$@"; }
  mysql_dump() { docker exec -i "$CONTAINER" mysqldump -u"$DB_USER" -p"$DB_PASSWORD" --no-data --skip-comments --skip-dump-date "$DB_NAME"; }
elif command -v mysqldump >/dev/null 2>&1; then
  MODE="local"
  mysql_do()   { MYSQL_PWD="$DB_PASSWORD" mysql -h"$DB_HOST" -P"$DB_PORT" -u"$DB_USER" "$DB_NAME" "$@"; }
  mysql_dump() { MYSQL_PWD="$DB_PASSWORD" mysqldump -h"$DB_HOST" -P"$DB_PORT" -u"$DB_USER" --no-data --skip-comments --skip-dump-date "$DB_NAME"; }
else
  fail "ГЕЙТ НЕ ЗАПУЩЕН: нет ни контейнера '$CONTAINER', ни клиента mysqldump"
  exit $RC_NOT_RUN
fi

mysql_do -e 'SELECT 1' >/dev/null 2>&1 || {
  fail "ГЕЙТ НЕ ЗАПУЩЕН: база '$DB_NAME' недоступна ($MODE)"
  exit $RC_NOT_RUN
}

say "Режим доступа к MySQL: $MODE"

SNAP_A="$ROOT/deploy/.schema-snapshot-a.sql"
SNAP_B="$ROOT/deploy/.schema-snapshot-b.sql"

normalize() {
  # Канонизация вынесена в отдельный скрипт: убирает комментарии, директивы,
  # AUTO_INCREMENT и служебную alembic_version, а внутри CREATE TABLE
  # сортирует строки индексов, сохраняя порядок колонок.
  "$PY" "$ROOT/deploy/normalize-schema.py"
}

count_tables() {
  mysql_do -N -B -e \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$DB_NAME'" 2>/dev/null \
    | tr -d '[:space:]'
}

step() { say ""; say "── $* ──"; }

# --- 0. Необязательный сброс до пустой схемы --------------------------------
#
# Нужен, когда прошлый прогон упал на середине: часть таблиц снесена, а версия
# в alembic_version осталась прежней — тогда откат честно падает на «Unknown
# table», и починить это изнутри цикла нечем.
#
# Сброс умеет разрушать, поэтому запрещён везде, кроме заведомо одноразовой
# базы: контейнер разработки или явное ALLOW_DESTRUCTIVE=1. Просить
# внимательности бесполезно.
if [ "${1:-}" = "--reset" ]; then
  if [ "$MODE" != "docker" ] && [ "${ALLOW_DESTRUCTIVE:-0}" != "1" ]; then
    fail "--reset запрещён: это не контейнер разработки. ALLOW_DESTRUCTIVE=1, если уверены."
    exit $RC_NOT_RUN
  fi
  EXISTING=$(count_tables)
  say "⚠️  Сброс схемы '$DB_NAME' ($MODE): было таблиц — $EXISTING"
  mysql_do -N -B -e "
    SET FOREIGN_KEY_CHECKS = 0;
    SET @sql := (
      SELECT IFNULL(CONCAT('DROP TABLE ', GROUP_CONCAT('\`', table_name, '\`')), 'SELECT 1')
      FROM information_schema.tables WHERE table_schema = '$DB_NAME'
    );
    PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
    SET FOREIGN_KEY_CHECKS = 1;
  " >/dev/null 2>&1 || { fail "сброс не удался"; exit $RC_NOT_RUN; }
  say "✓ схема пуста"
fi

# --- 1. Статика цепи --------------------------------------------------------

step "1/7 Статика цепи миграций"
HEADS=$("$ALEMBIC" heads 2>/dev/null | grep -c 'head' || true)
if [ "$HEADS" -ne 1 ]; then
  fail "В цепи миграций $HEADS голов вместо одной — ветки разошлись"
  exit 1
fi
say "✓ одна голова"

# --- 2. Полный накат --------------------------------------------------------

step "2/7 Полный накат"
# Сначала откатываем до нуля. Без этого шаг ничего не проверяет, когда база уже
# на голове: `upgrade head` молча ничего не делает, снимок A снимается со СТАРОЙ
# схемы, и правка миграции сравнивается сама с собой предыдущей версии.
# Именно так этот шаг однажды показал расхождение, которого в коде не было.
"$ALEMBIC" downgrade base >/dev/null 2>&1 || {
  fail "не удалось откатить базу до нуля перед прогоном"
  exit 1
}
[ "$(count_tables)" -le 1 ] || { fail "после отката до нуля остались таблицы"; exit 1; }

"$ALEMBIC" upgrade head >/dev/null 2>&1 || { fail "накат упал"; exit 1; }
TABLES_A=$(count_tables)
say "✓ таблиц после наката: $TABLES_A"
[ "${TABLES_A:-0}" -gt 1 ] || { fail "после наката нет таблиц — накат ничего не сделал"; exit 1; }

mysql_dump 2>/dev/null | normalize > "$SNAP_A" || { fail "снимок схемы не снялся"; exit 1; }
LINES_A=$(wc -l < "$SNAP_A" | tr -d ' ')
say "✓ снимок схемы: $LINES_A строк"
[ "$LINES_A" -gt 50 ] || { fail "снимок подозрительно короткий — сверять нечего"; exit 1; }

# --- 3. Повтор наката: обязан быть no-op ------------------------------------

step "3/7 Повторный накат (ожидается no-op)"
"$ALEMBIC" upgrade head >/dev/null 2>&1 || { fail "повторный накат упал"; exit 1; }
TABLES_REPEAT=$(count_tables)
[ "$TABLES_REPEAT" = "$TABLES_A" ] || {
  fail "повторный накат изменил схему: было $TABLES_A таблиц, стало $TABLES_REPEAT"
  exit 1
}
say "✓ повтор ничего не изменил"

# --- 4. Полный откат --------------------------------------------------------

step "4/7 Полный откат"
"$ALEMBIC" downgrade base 2>&1 | tail -5 | sed 's/^/    /'
DOWN_RC=${PIPESTATUS[0]}
[ "$DOWN_RC" -eq 0 ] || { fail "откат упал (код $DOWN_RC)"; exit 1; }

TABLES_DOWN=$(count_tables)
# Остаться должна только служебная alembic_version.
[ "${TABLES_DOWN:-99}" -le 1 ] || {
  fail "после отката осталось $TABLES_DOWN таблиц вместо одной служебной"
  mysql_do -N -B -e "SELECT table_name FROM information_schema.tables WHERE table_schema='$DB_NAME'" | sed 's/^/    осталась: /'
  exit 1
}
say "✓ откат вычистил схему"

# --- 5. Повторный накат -----------------------------------------------------

step "5/7 Повторный накат после отката"
"$ALEMBIC" upgrade head >/dev/null 2>&1 || { fail "накат после отката упал"; exit 1; }
mysql_dump 2>/dev/null | normalize > "$SNAP_B" || { fail "второй снимок не снялся"; exit 1; }
say "✓ схема пересоздана"

# --- 6. Сверка снимков ------------------------------------------------------

step "6/7 Сверка снимков схемы"
if ! diff -u "$SNAP_A" "$SNAP_B" > "$ROOT/deploy/.schema-diff.txt"; then
  fail "схема после отката и повторного наката разошлась. Отличия:"
  head -60 "$ROOT/deploy/.schema-diff.txt" >&2
  say "Полный список: deploy/.schema-diff.txt"
  exit 1
fi
rm -f "$ROOT/deploy/.schema-diff.txt"
say "✓ схема после отката и повторного наката совпала побайтно"

# --- 7. Сверка миграции с моделями -----------------------------------------
#
# Миграция и модели обязаны описывать одну и ту же схему. Расхождение между
# ними — самый тихий вид поломки: код читает колонку, которой на проде нет.
# Сверяем не чтением кода, а дампами: типы всех колонок, индексы, внешние
# ключи и коллации.

step "7/7 Сверка миграции с моделями"
CHECK_DB="${SHOPBOT_SCHEMA_CHECK_DB:-shopbot_schema_check}"

if ! "$PY" deploy/build-reference-schema.py >/dev/null 2>&1; then
  fail "ГЕЙТ НЕ ЗАПУЩЕН: не удалось построить эталонную схему из моделей"
  say "  Нужна база '$CHECK_DB' и права на неё:"
  say "  docker exec -i $CONTAINER mysql -uroot -pdevroot < deploy/mysql-init/01-grants.sql"
  exit $RC_NOT_RUN
fi

SNAP_MODELS="$ROOT/deploy/.schema-snapshot-models.sql"
if [ "$MODE" = "docker" ]; then
  docker exec -i "$CONTAINER" mysqldump -u"$DB_USER" -p"$DB_PASSWORD" \
    --no-data --skip-comments --skip-dump-date "$CHECK_DB" 2>/dev/null \
    | normalize > "$SNAP_MODELS"
else
  MYSQL_PWD="$DB_PASSWORD" mysqldump -h"$DB_HOST" -P"$DB_PORT" -u"$DB_USER" \
    --no-data --skip-comments --skip-dump-date "$CHECK_DB" 2>/dev/null \
    | normalize > "$SNAP_MODELS"
fi

# Оба дампа уже прошли через normalize, поэтому сравниваются как есть.
cp "$SNAP_A" "$ROOT/deploy/.cmp-migrated.sql"
cp "$SNAP_MODELS" "$ROOT/deploy/.cmp-models.sql"

if diff -u "$ROOT/deploy/.cmp-models.sql" "$ROOT/deploy/.cmp-migrated.sql" \
     > "$ROOT/deploy/.schema-model-diff.txt"; then
  rm -f "$ROOT/deploy/.schema-model-diff.txt"
  say "✓ схема из миграции совпадает со схемой из моделей"
  say ""
  say "✅ Миграции проверены: накат → повтор → откат → накат → сверка с моделями."
  say "   Таблиц: $TABLES_A, строк схемы: $LINES_A."
  exit 0
fi

fail "миграция разошлась с моделями (слева модели, справа миграция):"
head -60 "$ROOT/deploy/.schema-model-diff.txt" >&2
say "Полный список: deploy/.schema-model-diff.txt"
say "Обычно лечится: alembic revision --autogenerate -m \"...\""
exit 1
