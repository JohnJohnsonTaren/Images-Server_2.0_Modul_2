# Сервер зображень 2.0

Веб-сервіс для завантаження зображень. Python-бекенд + PostgreSQL + Nginx, усе запускається через Docker Compose.

## Запуск

```bash
docker compose up --build
```

Сайт: http://localhost:8080

## Структура проєкту

```
├── app.py              # Python-бекенд
├── requirements.txt    # Залежності Python
├── Dockerfile          # Образ бекенду
├── docker-compose.yml  # app + nginx + db (PostgreSQL)
├── nginx.conf          # Конфігурація Nginx
├── backup.sh           # Скрипт резервного копіювання
├── images/             # Завантажені зображення (volume)
├── logs/               # app.log (volume)
├── backups/            # Резервні копії бази даних
└── static/             # HTML/CSS/JS
```

## Маршрути

| Маршрут | Метод | Опис |
|---|---|---|
| `/` | GET | Головна сторінка |
| `/upload` | POST | Завантаження зображення (jpg, png, gif, до 5 МБ). Метадані зберігаються в PostgreSQL. Якщо запис у БД не вдався, файл видаляється |
| `/images-list?page=N` | GET | Список зображень, по 10 на сторінці, новіші першими. Кнопки «Попередня сторінка» / «Наступна сторінка» вимикаються на краях списку |
| `/delete/<id>` | GET | Видаляє запис з БД і файл з `/images`, потім перенаправляє на `/images-list`. Якщо id не існує, повертає 404 |
| `/images/<файл>` | GET | Перегляд файлу (віддає Nginx) |

## База даних

Таблиця `images` створюється автоматично при старті бекенду:

| Поле | Тип | Опис |
|---|---|---|
| `id` | SERIAL PK | Ідентифікатор |
| `filename` | TEXT | Згенерована назва файлу |
| `original_name` | TEXT | Оригінальна назва від користувача |
| `size` | INTEGER | Розмір у байтах |
| `upload_time` | TIMESTAMP | Час завантаження |
| `file_type` | TEXT | Формат (jpg, png, gif) |

## Логування

Усі дії (завантаження, видалення, помилки, резервне копіювання) записуються у `logs/app.log` з датою, часом і результатом.

## Резервне копіювання

Створити копію (з кореня проєкту, коли контейнери запущені):

```bash
chmod +x backup.sh   # один раз
./backup.sh
```

Файл зберігається як `backups/backup_<дата>_<час>.sql`, наприклад `backups/backup_2025-01-24_153000.sql`.

Те саме вручну:

```bash
docker exec postgres_container pg_dump -U images_user images_hosting > backups/backup_2025-01-24_153000.sql
```

## Відновлення з копії

```bash
docker exec -i postgres_container psql -U images_user images_hosting < backups/backup_2025-01-24_153000.sql
```
