import psycopg
import  time

from psycopg import Connection


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
        connection = psycopg.connect("postgres://images_user:1234567890@db:5432/images_hosting")
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

print("Connected")