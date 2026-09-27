FROM python:3.10.21-alpine3.23
WORKDIR /server
COPY . .
RUN pip install -r requirements.txt
CMD ["python3", "app.py"]