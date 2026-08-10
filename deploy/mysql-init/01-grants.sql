-- Права для окружения разработки.
--
-- Тестам нужна своя база (`shopbot_test`), а проверке миграций — ещё одна
-- (`shopbot_schema_check`) под эталонную схему. Обе одноразовые и живут только
-- на машине разработчика.
--
-- На проде этих прав у бота быть не должно: приложению незачем уметь создавать
-- и удалять базы.

CREATE DATABASE IF NOT EXISTS `shopbot_test`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS `shopbot_schema_check`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

GRANT ALL PRIVILEGES ON `shopbot_test`.* TO 'shopbot'@'%';
GRANT ALL PRIVILEGES ON `shopbot_schema_check`.* TO 'shopbot'@'%';
FLUSH PRIVILEGES;
