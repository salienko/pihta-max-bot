FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py *.sql content.json rules.txt ./
COPY assets ./assets
RUN useradd --create-home bot
RUN mkdir -p /app/data/backups && chown -R bot:bot /app/data
USER bot
EXPOSE 3000
CMD ["python", "bot.py"]
