FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hotel_mcp ./hotel_mcp
EXPOSE 8120
CMD ["python", "-m", "hotel_mcp", "--config", "/data/config.json", "--database", "/data/hotel.sqlite3", "--host", "0.0.0.0"]
