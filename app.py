import http
import json
import logging
import os
import re
import socketserver
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import psycopg
from psycopg import Connection

# ----------------------------------------------------------------------
# Конфіги
# ----------------------------------------------------------------------
HOST = "0.0.0.0"
PORT = 8000

IMAGES_DIR = "/images"
LOGS_DIR = "/logs"

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
    logger.info("Помилка: %s", message)

# ----------------------------------------------------------------------
# Конекшин Server
# ----------------------------------------------------------------------
def insert_image_metadata(
            connection: Connection,
            filename: str,
            original_name: str,
            size: int,
            file_type: str
    ):
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO images (filename, original_name,size,file_type) VALUES (%s, %s, %s, %s) RETURNING id;"
                [filename, original_name, size, file_type]
            )
            return cursor.fetchone()


def get_images_metadata(connection: Connection, page=1):
    offset = 10 * (page - 1)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM images OFFSET %s LIMIT 10;",
            [offset]
        )
        return cursor.fetchall()


def delete_images_metadata(connection: Connection, id: int):
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM images WHERE id = %s;",
            [id]
        )


connection = None
while not connection:
    try:
        connection = psycopg.connect("postgres://images_user:1234567890@db:5432/images_hosting", autocommit=True)
    except Exception as e:
        print("Db unavailable... Retrying...")
        time.sleep(1)

with connection.cursor() as cursor:
    cursor.execute(
        """
            CREATE TABLE IF NOT EXISTS images (
                id SERIAL PRIMARY KEY,
                filename TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                size INTEGER NOT NULL,
                upload_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                file_type TEXT NOT NULL);
        """
    )

# ----------------------------------------------------------------------
# Допоміжні функції
# ----------------------------------------------------------------------
def generate_unique_filename(original_name: str) -> str:
    ext = os.path.splitext(original_name)[1].lower().lstrip(".")
    return f"{uuid.uuid4().hex}{ext}"


class MultipartParseError(Exception):
    """Файл не знайдено або тіло запиту некоректне."""


def parse_multipart(body: bytes, boundary: str) -> dict:
    """Мінімальний парсер multipart/form-data для одного файлового поля."""
    boundary_bytes = ("--" + boundary).encode()

    for part in body.split(boundary_bytes):
        part = part.strip(b"\r\n")
        if not part or part == b"--" or b"\r\n\r\n" not in part:
            continue

        headers_raw, content = part.split(b"\r\n\r\n", 1)
        headers_text = headers_raw.decode(errors="ignore")

        match = re.search(
            r'Content-Disposition:.*name="([^"]+)"(?:; filename="([^"]*)")?',
            headers_text,
            re.IGNORECASE,
        )
        if match and match.group(2):
            return {"filename": match.group(2), "data": content.rstrip(b"\r\n")}

    raise MultipartParseError("Файл не знайдено у запиті")

# ----------------------------------------------------------------------
# HTTP-обробник
# ----------------------------------------------------------------------
class ImageUploaderHandler(http.server.BaseHTTPRequestHandler):
    server_version = "ImageUploaderServer/1.0"

    def log_message(self, format, *args):
        pass  # використовуємо власний logger замість стандартного виводу

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --------- GET / ---------
    def do_GET(self) -> None:
        if urlparse(self.path).path == "/":
            self._send_json(200, {
                "message": "Ласкаво просимо до сервісу зберігання зображень!",
                "upload": "POST /upload",
                "images": "/images/<ім'я_файлу> (обслуговується Nginx)",
            })
        else:
            self._send_json(404, {"error": "Маршрут не знайдено"})

    # --------- POST /upload ---------
    def do_POST(self) -> None:
        if urlparse(self.path).path == "/upload":
            self._handle_upload()
        else:
            self._send_json(404, {"error": "Маршрут не знайдено"})

    def _handle_upload(self, MAX_CONTENT_LENGTH=None) -> None:
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
        ext = os.path.splitext(original_name)[1].lower()

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

        try:
            with open(os.path.join(IMAGES_DIR, new_filename), "wb") as f:
                f.write(file_data)
        except OSError as exc:
            log_error(f"Не вдалося зберегти файл {original_name}: {exc}")
            self._send_json(500, {"error": "Внутрішня помилка сервера"})
            return

        log_success(f"Зображення {new_filename} завантажено")
        self._send_json(200, {
            "message": "Файл успішно завантажено",
            "file_id": new_filename,
            "url": f"/images/{new_filename}",
        })


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Дозволяє обробляти декілька одночасних завантажень (до 10 користувачів)."""
    daemon_threads = True


def run() -> None:
    server = ThreadingHTTPServer((HOST, PORT), ImageUploaderHandler)
    logger.info(f"Сервер запущено на {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Сервер зупинено користувачем")
        server.shutdown()


if __name__ == "__main__":
    run()