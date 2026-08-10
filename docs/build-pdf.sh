#!/usr/bin/env bash
#
# Пересобирает PDF-инструкцию из docs/ЗАПУСК.html.
#
# PDF в репозитории не хранится: это двоичный файл, который git не умеет
# сравнивать, а каждая пересборка давала бы «изменение» всего файла целиком.
# Исходник — HTML, он читается и правится как обычный текст.
#
#   docs/build-pdf.sh [куда_положить.pdf]
#
set -uo pipefail
cd "$(dirname "$0")/.." || exit 3

OUT="${1:-$HOME/Desktop/Запуск бота — инструкция.pdf}"
SRC="$PWD/docs/ЗАПУСК.html"

[ -f "$SRC" ] || { echo "✗ нет исходника: $SRC" >&2; exit 3; }

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ ! -x "$CHROME" ]; then
  CHROME=$(command -v chromium || command -v google-chrome || true)
fi
[ -n "$CHROME" ] && [ -x "$CHROME" ] || {
  echo "✗ СБОРКА НЕ СОСТОЯЛАСЬ: не нашли Chrome или Chromium." >&2
  echo "  Откройте docs/ЗАПУСК.html в браузере и напечатайте в PDF вручную." >&2
  exit 3
}

"$CHROME" --headless --disable-gpu --no-pdf-header-footer \
  --print-to-pdf="$OUT" "file://$SRC" >/dev/null 2>&1

[ -s "$OUT" ] || { echo "✗ PDF не собрался" >&2; exit 1; }
head -c 5 "$OUT" | grep -q '%PDF-' || { echo "✗ получился не PDF" >&2; exit 1; }

echo "✓ готово: $OUT ($(du -h "$OUT" | cut -f1))"
