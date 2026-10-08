#!/usr/bin/env bash
# Резервне копіювання PostgreSQL. Запуск з кореня проєкту: ./backup.sh
set -uo pipefail
cd "$(dirname "$0")"

mkdir -p backups logs
STAMP="$(date +%Y-%m-%d_%H%M%S)"
FILE="backups/backup_${STAMP}.sql"

if docker exec postgres_container pg_dump -U images_user images_hosting > "$FILE"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Успіх: резервну копію створено ($FILE)" | tee -a logs/app.log
else
    rm -f "$FILE"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Помилка: не вдалося створити резервну копію" | tee -a logs/app.log
    exit 1
fi

# Відновлення:
# docker exec -i postgres_container psql -U images_user images_hosting < backups/backup_<дата>.sql