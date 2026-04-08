FROM python:3.12-slim

WORKDIR /app

# Install system deps for Couchbase Lite C and CFFI
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        wget gcc libffi-dev git ca-certificates zlib1g-dev && \
    rm -rf /var/lib/apt/lists/*

# Download and install Couchbase Lite C CE 3.2.1
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then CBL_ARCH="x86_64"; CBL_LIB="x86_64-linux-gnu"; \
    else CBL_ARCH="arm64"; CBL_LIB="aarch64-linux-gnu"; fi && \
    wget -q "https://packages.couchbase.com/releases/couchbase-lite-c/3.2.1/couchbase-lite-c-community-3.2.1-linux-${CBL_ARCH}.tar.gz" \
        -O /tmp/cblite.tar.gz && \
    mkdir -p /opt/cblite && \
    tar xzf /tmp/cblite.tar.gz -C /opt/cblite --strip-components=1 && \
    cp /opt/cblite/lib/${CBL_LIB}/libcblite.so* /usr/local/lib/ && \
    cp -r /opt/cblite/include/* /usr/local/include/ && \
    ldconfig && \
    rm -rf /tmp/cblite.tar.gz /opt/cblite

# Clone and build Couchbase Lite Python bindings
RUN pip install --no-cache-dir cffi setuptools && \
    git clone --depth 1 https://github.com/couchbaselabs/couchbase-lite-python.git /opt/cbl-python && \
    cd /opt/cbl-python/CouchbaseLite && \
    python3 ../build.py --include /usr/local/include --library /usr/local/lib/libcblite.so

ENV PYTHONPATH="/opt/cbl-python:${PYTHONPATH}"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/uploads /app/data

EXPOSE 8888

CMD ["python", "app.py"]
