FROM python:3.10-slim
WORKDIR /app

RUN rm -f /etc/apt/apt.conf.d/docker-clean; echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache


COPY requirements.txt .

ENV PIP_INDEX_URL=https://mirror-pypi.runflare.com/simple \
    PIP_TRUSTED_HOST=mirror-pypi.runflare.com \
    PIP_TIMEOUT=60

RUN --mount=type=cache,target=/root/.cache/pip,id=pip-cache \
    pip install -r requirements.txt

# RUN pip install --no-cache-dir -r requirements.txt
# RUN pip install -r requirements.txt


COPY . .
CMD ["python", "main.py"]
