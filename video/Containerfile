FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
EXPOSE 8111
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8111", "--log-level", "info"]
