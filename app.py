import html
import http.server
import json
import logging
import math
import os
import re
import socketserver
import time
import uuid
from urllib.parse import urlparse, parse_qs

import psycopg

# ----------------------------------------------------------------------
# Конфіги
# ----------------------------------------------------------------------
HOST = "0.0.0.0"
PORT = 8000

IMAGES_DIR = "/images"
LOGS_DIR = "/logs"
PAGE_SIZE = 10

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgres://images_user:1234567890@db:5432/images_hosting",
)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}
MAX_CONTENT_LENGTH = 5 * 1024 * 1024

os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# ----------------------------------------------------------------------
# Логування
# ----------------------------------------------------------------------
logger = logging.getLogger("image_uploader")
logger.setLevel(logging.INFO)

_formatter = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_file_handler = logging.FileHandler(os.path.join(LOGS_DIR, "app.log"), encoding="utf-8")
_file_handler.setFormatter(_formatter)
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)
logger.addHandler(_console_handler)


def log_success(message: str) -> None:
    logger.info("Успіх: %s", message)


def log_error(message: str) -> None:
    logger.error("Помилка: %s", message)


# ----------------------------------------------------------------------
# База даних
# ----------------------------------------------------------------------
def get_connection() -> psycopg.Connection:
    """Нове з'єднання на кожен запит (безпечно для багатопотокового сервера)."""
    return psycopg.connect(DATABASE_URL, autocommit=True)


def init_db() -> None:
    while True:
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS images (
                        id SERIAL PRIMARY KEY,
                        filename TEXT NOT NULL,
                        original_name TEXT NOT NULL,
                        size INTEGER NOT NULL,
                        upload_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        file_type TEXT NOT NULL
                    );
                    """
                )
                # міграція зі старої назви колонки
                cur.execute(
                    """
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'images'
                              AND column_name = 'original_filename'
                        ) THEN
                            ALTER TABLE images
                                RENAME COLUMN original_filename TO original_name;
                        END IF;
                    END $$;
                    """
                )
            log_success("З'єднання з базою даних встановлено, таблицю images готово")
            return
        except Exception as exc:
            print(f"Db unavailable... Retrying... ({exc})")
            time.sleep(1)


def save_metadata(filename, original_name, size, file_type):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO images (filename, original_name, size, file_type) "
            "VALUES (%s, %s, %s, %s) RETURNING id;",
            (filename, original_name, size, file_type),
        )
        return cur.fetchone()[0]


def count_images() -> int:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM images;")
        return cur.fetchone()[0]


def get_images_page(page: int):
    offset = PAGE_SIZE * (page - 1)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, filename, original_name, size, upload_time, file_type "
            "FROM images ORDER BY upload_time DESC, id DESC LIMIT %s OFFSET %s;",
            (PAGE_SIZE, offset),
        )
        return cur.fetchall()


def get_image_by_id(image_id: int):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, filename FROM images WHERE id = %s;", (image_id,))
        return cur.fetchone()


def delete_image_row(image_id: int) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM images WHERE id = %s;", (image_id,))


# ----------------------------------------------------------------------
# Допоміжні функції
# ----------------------------------------------------------------------
def generate_unique_filename(original_name: str) -> str:
    ext = os.path.splitext(original_name)[1].lower().lstrip(".")
    return f"{uuid.uuid4().hex}.{ext}"


class MultipartParseError(Exception):
    """Файл не знайдено або тіло запиту некоректне."""


def parse_multipart(body: bytes, boundary: str) -> dict:
    """Мінімальний парсер multipart/form-data для одного файлового поля."""
    delimiter = b"--" + boundary.encode()

    for part in body.split(delimiter):
        if b"\r\n\r\n" not in part:
            continue

        headers_raw, content = part.split(b"\r\n\r\n", 1)
        headers_text = headers_raw.decode("utf-8", errors="ignore")

        match = re.search(r'filename="([^"]*)"', headers_text, re.IGNORECASE)
        if not match:
            continue

        if content.endswith(b"\r\n"):
            content = content[:-2]

        return {"filename": match.group(1), "data": content}

    raise MultipartParseError("Файл не знайдено у запиті")


def render_images_page(rows, page: int, total_pages: int) -> str:
    if rows:
        body_rows = []
        for image_id, filename, original_name, size, upload_time, file_type in rows:
            safe_file = html.escape(filename)
            body_rows.append(
                "<tr>"
                f'<td><a href="/images/{safe_file}" target="_blank">{safe_file}</a></td>'
                f"<td>{html.escape(original_name)}</td>"
                f"<td>{size / 1024:.1f}</td>"
                f"<td>{upload_time.strftime('%Y-%m-%d %H:%M:%S')}</td>"
                f"<td>{html.escape(file_type)}</td>"
                f'<td><a class="btn btn--danger" href="/delete/{image_id}" '
                f"onclick=\"return confirm('Видалити зображення?');\">Видалити</a></td>"
                "</tr>"
            )
        table = (
            "<table><thead><tr>"
            "<th>Назва файлу</th><th>Оригінальна назва</th><th>Розмір (КБ)</th>"
            "<th>Дата завантаження</th><th>Тип файлу</th><th></th>"
            "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>"
        )
    else:
        table = '<p class="empty">Немає завантажених зображень</p>'

    if page > 1:
        prev_btn = f'<a class="btn" href="/images-list?page={page - 1}">Попередня сторінка</a>'
    else:
        prev_btn = '<span class="btn btn--disabled">Попередня сторінка</span>'

    if page < total_pages:
        next_btn = f'<a class="btn" href="/images-list?page={page + 1}">Наступна сторінка</a>'
    else:
        next_btn = '<span class="btn btn--disabled">Наступна сторінка</span>'

    return f"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Список зображень</title>
<style>
  body {{ font-family: Inter, Arial, sans-serif; background: #151515; color: #eee; margin: 0; padding: 32px; }}
  h1 {{ margin-top: 0; }}
  table {{ width: 100%; border-collapse: collapse; background: #1f1f1f; }}
  th, td {{ padding: 10px 14px; border-bottom: 1px solid #333; text-align: left; }}
  th {{ background: #2a2a2a; }}
  tr:hover td {{ background: #262626; }}
  a {{ color: #7ab7ff; }}
  .btn {{ display: inline-block; padding: 8px 14px; border-radius: 6px; background: #3b82f6;
          color: #fff; text-decoration: none; }}
  .btn--danger {{ background: #dc2626; }}
  .btn--disabled {{ background: #444; color: #888; cursor: not-allowed; }}
  .pagination {{ display: flex; gap: 12px; align-items: center; margin-top: 20px; }}
  .empty {{ text-align: center; margin-top: 50px; color: #aaa; }}
  .top {{ margin-bottom: 20px; }}
</style>
</head>
<body>
<h1>Список зображень</h1>
<div class="top"><a href="/form/upload.html">&larr; Завантажити нове</a></div>
{table}
<div class="pagination">
  {prev_btn}
  <span>Сторінка {page} з {total_pages}</span>
  {next_btn}
</div>
</body>
</html>"""


# ----------------------------------------------------------------------
# HTTP-обробник
# ----------------------------------------------------------------------
class ImageUploaderHandler(http.server.BaseHTTPRequestHandler):
    server_version = "ImageUploaderServer/2.0"

    def log_message(self, format, *args):
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --------- GET ---------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if path == "/":
                self._send_json(200, {
                    "message": "Ласкаво просимо до сервісу зберігання зображень!",
                    "upload": "POST /upload",
                    "list": "GET /images-list?page=1",
                    "delete": "GET /delete/<id>",
                    "images": "/images/<ім'я_файлу> (обслуговується Nginx)",
                })
            elif path == "/images-list":
                self._handle_images_list(parse_qs(parsed.query))
            elif re.fullmatch(r"/delete/\d+", path):
                self._handle_delete(int(path.rsplit("/", 1)[1]))
            else:
                self._send_json(404, {"error": "Маршрут не знайдено"})
        except Exception:
            logger.exception("Необроблена помилка в GET %s", path)
            self._send_json(500, {"error": "Внутрішня помилка сервера"})

    def _handle_images_list(self, query: dict) -> None:
        try:
            page = int(query.get("page", ["1"])[0])
        except ValueError:
            page = 1
        page = max(page, 1)

        try:
            total = count_images()
            total_pages = max(1, math.ceil(total / PAGE_SIZE))
            page = min(page, total_pages)
            rows = get_images_page(page)
        except Exception:
            logger.exception("Помилка читання списку зображень з БД")
            self._send_json(500, {"error": "Помилка бази даних"})
            return

        self._send_html(200, render_images_page(rows, page, total_pages))

    def _handle_delete(self, image_id: int) -> None:
        try:
            row = get_image_by_id(image_id)
        except Exception:
            logger.exception("Помилка БД при видаленні id=%s", image_id)
            self._send_json(500, {"error": "Помилка бази даних"})
            return

        if row is None:
            log_error(f"Видалення: зображення з id={image_id} не знайдено")
            self._send_json(404, {"error": f"Зображення з id={image_id} не знайдено"})
            return

        _, filename = row

        try:
            delete_image_row(image_id)
        except Exception:
            logger.exception("Не вдалося видалити запис id=%s", image_id)
            self._send_json(500, {"error": "Помилка бази даних"})
            return

        # захист від path traversal
        file_path = os.path.join(IMAGES_DIR, os.path.basename(filename))
        try:
            os.remove(file_path)
            log_success(f"Зображення {filename} (id={image_id}) видалено")
        except FileNotFoundError:
            log_error(f"Файл {filename} (id={image_id}) відсутній на диску; запис у БД видалено")
        except OSError as exc:
            log_error(f"Не вдалося видалити файл {filename}: {exc}")

        self._redirect("/images-list")

    # --------- POST /upload ---------
    def do_POST(self) -> None:
        if urlparse(self.path).path == "/upload":
            try:
                self._handle_upload()
            except Exception:
                logger.exception("Необроблена помилка в /upload")
                self._send_json(500, {"error": "Внутрішня помилка сервера"})
        else:
            self._send_json(404, {"error": "Маршрут не знайдено"})

    def _handle_upload(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))

        if content_length <= 0:
            log_error("Отримано порожнє тіло запиту на /upload")
            self._send_json(400, {"error": "Порожній запит"})
            return

        if content_length > MAX_CONTENT_LENGTH:
            log_error(f"Перевищено допустимий розмір запиту ({content_length} байт)")
            self._send_json(400, {"error": "Файл перевищує максимальний розмір 5 МБ"})
            return

        content_type = self.headers.get("Content-Type", "")
        boundary_match = re.search(r"boundary=(.+)", content_type)
        if "multipart/form-data" not in content_type or not boundary_match:
            log_error("Некоректний Content-Type у запиті на /upload")
            self._send_json(400, {"error": "Очікується multipart/form-data"})
            return

        body = self.rfile.read(content_length)

        try:
            file_part = parse_multipart(body, boundary_match.group(1).strip('"'))
        except MultipartParseError:
            log_error("Файл не знайдено у формі завантаження")
            self._send_json(400, {"error": "Файл не знайдено у запиті"})
            return

        original_name = file_part["filename"] or "unnamed"
        file_data = file_part["data"]
        ext = os.path.splitext(original_name)[1].lower().lstrip(".")

        if ext not in ALLOWED_EXTENSIONS:
            log_error(f"Непідтримуваний формат файлу ({original_name})")
            self._send_json(400, {"error": f"Непідтримуваний формат файлу: {ext or 'невідомий'}"})
            return

        if not file_data:
            log_error(f"Файл {original_name} порожній")
            self._send_json(400, {"error": "Завантажений файл порожній"})
            return

        if len(file_data) > MAX_CONTENT_LENGTH:
            log_error(f"Файл {original_name} перевищує 5 МБ")
            self._send_json(400, {"error": "Файл перевищує максимальний розмір 5 МБ"})
            return

        new_filename = generate_unique_filename(original_name)
        file_path = os.path.join(IMAGES_DIR, new_filename)

        try:
            with open(file_path, "wb") as f:
                f.write(file_data)
        except OSError as exc:
            log_error(f"Не вдалося зберегти файл {original_name}: {exc}")
            self._send_json(500, {"error": "Внутрішня помилка сервера"})
            return

        try:
            save_metadata(new_filename, original_name, len(file_data), ext)
        except Exception as exc:
            log_error(f"Не вдалося записати метадані {original_name} в БД: {exc}")
            try:
                os.remove(file_path)  # файл не повинен залишатися без запису в БД
            except OSError:
                pass
            self._send_json(500, {"error": "Помилка бази даних"})
            return

        log_success(f"Зображення {original_name} збережено як {new_filename}")
        self._send_json(200, {
            "message": "Файл успішно завантажено",
            "file_id": new_filename,
            "url": f"/images/{new_filename}",
        })


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Дозволяє обробляти декілька одночасних запитів."""
    daemon_threads = True


def run() -> None:
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), ImageUploaderHandler)
    logger.info(f"Сервер запущено на {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Сервер зупинено користувачем")
        server.shutdown()


if __name__ == "__main__":
    run()