FROM python:3.10-slim
WORKDIR /app
RUN pip install --no-cache-dir flask flask-cors requests python-dotenv influxdb-client
COPY . .
EXPOSE 5000
CMD ["python", "supervisor.py"]