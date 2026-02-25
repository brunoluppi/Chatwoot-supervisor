FROM python:3.10-slim
WORKDIR /app
RUN pip install flask flask-cors requests pyyaml python-dotenv influxdb-client
COPY supervisor.py .
CMD ["python", "supervisor.py"]