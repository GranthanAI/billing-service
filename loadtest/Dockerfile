# gRPC load driver for `make loadtest`; runs on the same Docker network as billing-api so the
# measurement is not dominated by Windows/WSL port forwarding.
FROM python:3.12-slim
RUN pip install --no-cache-dir "grpcio>=1.64" "protobuf>=5.26"
WORKDIR /app
COPY src/billing/gen /app/billing/gen
COPY src/billing/__init__.py /app/billing/__init__.py
COPY loadtest/drive.py /app/drive.py
ENV PYTHONPATH=/app
