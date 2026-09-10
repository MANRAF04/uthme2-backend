FROM python:3.11-slim

# Install OpenVPN, network tools, and timezone database for zoneinfo conversions
RUN apt-get update && apt-get install -y openvpn iproute2 tzdata && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Athens

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
