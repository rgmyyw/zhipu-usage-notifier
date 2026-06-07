FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
COPY main.py .
COPY config.example.json ./config.json

ENV CONFIG_PATH=/app/config.json
ENV TZ=Asia/Shanghai

CMD ["python", "-u", "main.py"]
